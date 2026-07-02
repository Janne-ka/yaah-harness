"""McpServer — newline-delimited JSON-RPC 2.0 loop speaking the MCP stdio protocol.

Who: an MCP client (Claude Code, Cursor, …) spawns the server process and
drives it over stdio; tests drive `serve_stdio` in-process over any
reader/writer pair with the same interface (readline / write+drain).
Where: yaah.adapters.mcp_server — the protocol half of the MCP operator
surface; the tool implementations live in tools.py.
Why: MCP's stdio transport is one JSON-RPC message per line. The loop is
deliberately serial (read line → dispatch → write line): yaah tool calls run
the engine in-process, and serializing them keeps the stdout-redirect window
(below) race-free.

Handled methods: `initialize`, `notifications/initialized` (and any other
notification: no reply), `ping`, `tools/list`, `tools/call`; everything else
answers -32601 method-not-found. A tool-level failure is NOT a protocol error:
it returns a result with `isError: true` (MCP convention), so the client's
model sees the message and can repair.

Stdout hygiene: the engine PRINTS to stdout (trace console sink is the default,
GATE/RESULT banners, "served:" in bus mode). In stdio mode stdout IS the
protocol channel, so every tool call runs under `redirect_stdout` into a
discard buffer; the writer holds the real transport directly and is unaffected.
Serial dispatch makes the global redirect safe across the tool's awaits.

Intended CLI wiring (cli.py is maintainer-owned; this module stays untouched by
it): a self-contained `mcp-serve` action —

    from .adapters.mcp_server import serve_process_stdio
    def _dispatch_mcp_serve(spec): asyncio.run(serve_process_stdio())
    # in _SELF_CONTAINED_DISPATCH: "mcp-serve": _dispatch_mcp_serve

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sys
from typing import Any, Dict, List, Optional

from .tools import TOOLS

PROTOCOL_VERSION = "2024-11-05"


def _server_version() -> str:
    """Installed wheel version, or an honest fallback for the source-checkout
    path. Kept in step with yaah.cli._resolve_version (tiny, sanctioned
    duplication — importing the CLI's privates would couple the surfaces)."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("yaah-harness")
        except PackageNotFoundError:
            return "(source checkout)"
    except ImportError:  # pragma: no cover - importlib.metadata is stdlib on 3.9+
        return "(unknown)"


def _error(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _result(msg_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


class McpServer:
    """One MCP server over one stream pair. Stateless between requests — every
    tool call re-loads its root config, so one server session can operate many
    pipelines (the root_path is a tool argument, not server state)."""

    def __init__(self, tools: Optional[List[Dict[str, Any]]] = None) -> None:
        self._tools = list(TOOLS if tools is None else tools)

    async def serve(self, reader: Any, writer: Any) -> None:
        """Read newline-delimited JSON-RPC until EOF. A malformed line answers
        -32700 and the loop KEEPS SERVING — one bad message must not kill the
        session."""
        while True:
            line = await reader.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                await self._send(writer, _error(None, -32700, "parse error: each message "
                                                              "must be one JSON object per line"))
                continue
            reply = await self._handle(msg)
            if reply is not None:
                await self._send(writer, reply)

    async def _send(self, writer: Any, obj: Dict[str, Any]) -> None:
        writer.write((json.dumps(obj, default=str) + "\n").encode("utf-8"))
        await writer.drain()

    async def _handle(self, msg: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
            return _error(msg.get("id") if isinstance(msg, dict) else None,
                          -32600, "invalid request: expected an object with a 'method'")
        method = msg["method"]
        msg_id = msg.get("id")
        if method == "initialize":
            return _result(msg_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "yaah", "version": _server_version()},
            })
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": [
                {"name": t["name"], "description": t["description"],
                 "inputSchema": t["inputSchema"]} for t in self._tools]})
        if method == "tools/call":
            return await self._call(msg_id, msg.get("params") or {})
        if method.startswith("notifications/"):
            return None  # notifications/initialized and friends: no reply, by spec
        if "id" not in msg:
            return None  # unknown NOTIFICATION: JSON-RPC forbids replying
        return _error(msg_id, -32601, "method not found: {!r}".format(method))

    async def _call(self, msg_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        tool = next((t for t in self._tools if t["name"] == name), None)
        if tool is None:
            return _error(msg_id, -32602, "unknown tool {!r}; available: {}".format(
                name, [t["name"] for t in self._tools]))
        arguments = params.get("arguments") or {}
        missing = [k for k in tool["inputSchema"].get("required", []) if k not in arguments]
        if missing:
            return _error(msg_id, -32602, "tool {!r} missing required argument(s): {}".format(
                name, missing))
        try:
            # stdout is the protocol channel — capture engine prints (see module
            # docstring). Serial dispatch makes the global redirect await-safe.
            with contextlib.redirect_stdout(io.StringIO()):
                out = await tool["handler"](arguments)
        except Exception as e:  # tool-level failure -> isError result, session lives on
            return _result(msg_id, {
                "content": [{"type": "text",
                             "text": "{}: {}".format(type(e).__name__, e)}],
                "isError": True,
            })
        return _result(msg_id, {
            "content": [{"type": "text", "text": json.dumps(out, default=str)}],
            "isError": False,
        })


async def serve_stdio(reader: Any, writer: Any) -> None:
    """The testable entry: serve MCP over any readline/write+drain pair (an
    asyncio StreamReader/StreamWriter, or in-memory doubles in tests)."""
    await McpServer().serve(reader, writer)


async def serve_process_stdio() -> None:
    """Wire serve_stdio onto the PROCESS's real stdin/stdout — what the CLI's
    `mcp-serve` action calls (see module docstring for the intended wiring)."""
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    w_transport, w_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout)
    writer = asyncio.StreamWriter(w_transport, w_protocol, reader, loop)
    await serve_stdio(reader, writer)
