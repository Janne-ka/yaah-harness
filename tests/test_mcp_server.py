"""yaah as an MCP server — in-process newline-delimited JSON-RPC 2.0 over
StreamReader + an in-memory writer (no subprocess).

Covers: the initialize handshake, notifications get no reply, ping, tools/list
exposes the five operator tools with schemas, validate on good/bad/missing
configs (an INVALID config is a successful validation — ok:false, isError
false), run on an offline fake pipeline, the full gate roundtrip over a durable
file store (run parks -> list_gates -> baton_schema -> resume completes),
protocol errors (unknown method, unknown tool, missing args, malformed line —
and the server KEEPS SERVING after each), and stdout hygiene (engine prints
must not leak onto the protocol channel).

Run: cd yaah && PYTHONPATH=src python3 tests/test_mcp_server.py
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile

from yaah.adapters.mcp_server import serve_stdio

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


class _CollectWriter:
    """The write+drain half of a stream pair; splits the byte stream on
    newlines and queues complete lines so the test awaits replies."""

    def __init__(self) -> None:
        self._buf = b""
        self.lines: "asyncio.Queue[bytes]" = asyncio.Queue()

    def write(self, data: bytes) -> None:
        self._buf += data
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            self.lines.put_nowait(line)

    async def drain(self) -> None:
        pass


class _Client:
    """Drives one server session: feed request lines in, await reply lines."""

    def __init__(self) -> None:
        self.reader = asyncio.StreamReader()
        self.writer = _CollectWriter()
        self.task: "asyncio.Task" = None  # type: ignore[assignment]
        self._id = 0

    def start(self) -> None:
        self.task = asyncio.ensure_future(serve_stdio(self.reader, self.writer))

    def send_line(self, text: str) -> None:
        self.reader.feed_data((text + "\n").encode("utf-8"))

    async def recv(self) -> dict:
        line = await asyncio.wait_for(self.writer.lines.get(), 30)
        return json.loads(line.decode("utf-8"))

    async def request(self, method: str, params: dict = None) -> dict:
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send_line(json.dumps(msg))
        resp = await self.recv()
        assert resp.get("id") == self._id, (msg, resp)
        return resp

    def notify(self, method: str) -> None:
        self.send_line(json.dumps({"jsonrpc": "2.0", "method": method}))

    async def close(self) -> None:
        self.reader.feed_eof()
        await asyncio.wait_for(self.task, 30)


async def _call(client: _Client, name: str, arguments: dict):
    """tools/call -> (isError, parsed-or-text). A tool result's content is one
    text item carrying a JSON document; parse it for the caller."""
    resp = await client.request("tools/call", {"name": name, "arguments": arguments})
    assert "error" not in resp, resp
    res = resp["result"]
    text = res["content"][0]["text"]
    assert res["content"][0]["type"] == "text", res
    if res.get("isError"):
        return True, text
    return False, json.loads(text)


# ---- fixtures (offline, deterministic — fake provider only) ------------------

LINEAR_PIPELINE = {
    "nodes": {
        "role:writer": {"type": "agent", "template": "write a spec for {{request}}",
                        "model": "fake:writer", "stage": "writer", "parse": False},
        "role:note": {"type": "transform", "target": "fn:mcp_noise_transform:shout",
                      "into": "note"},
    },
    "graph": {"start": "write", "stages": {
        "write": {"node": "role:writer", "then": "note"},
        "note": {"node": "role:note", "then": None},
    }},
}

# A user transform that PRINTS to stdout — realistic engine-adjacent noise the
# server MUST keep off the protocol channel (the stdout-hygiene falsifier:
# remove the server's redirect_stdout and the leak assertion below fails).
NOISE_TRANSFORM = (
    "def shout(args):\n"
    "    print('NOISE: a user transform printing to stdout')\n"
    "    return {'ok': True}\n"
)

GATED_PIPELINE = {
    "nodes": {
        "role:writer": {"type": "agent", "template": "write a spec for {{request}}",
                        "model": "fake:writer", "stage": "writer", "parse": False},
        "role:gate": {"type": "human_gate", "ask": "Approve this spec?\n{{raw}}",
                      "awaiting": "spec:approve", "form": "approve_or_revise"},
    },
    "graph": {"start": "write", "stages": {
        "write": {"node": "role:writer", "then": "gate"},
        "gate": {"node": "role:gate",
                 "branch": {"on": "decision", "routes": {"revise": "write"}}},
    }},
}


def _write_project(d: str, pipeline: dict, root_extra: dict = None) -> str:
    root = {
        "transport": {"type": "inproc"},
        "providers": {"fake": {"type": "fake", "default": "thinking"}},
        "default_provider": "fake",
        "state": {"type": "memory"},
        "pipeline": "pipeline.json",
        "input": {"request": "overdraft guard"},
        "run": True,
    }
    root.update(root_extra or {})
    json.dump(pipeline, open(os.path.join(d, "pipeline.json"), "w"))
    root_path = os.path.join(d, "root.json")
    json.dump(root, open(root_path, "w"))
    return root_path


# ---- scenarios ---------------------------------------------------------------

async def scenario_handshake_and_listing(client: _Client) -> None:
    resp = await client.request("initialize", {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"}})
    result = resp["result"]
    assert result["protocolVersion"] == "2024-11-05", result
    assert result["serverInfo"]["name"] == "yaah", result
    assert "tools" in result["capabilities"], result

    # a notification gets NO reply — proven by the very next reply belonging
    # to the ping, not the notification
    client.notify("notifications/initialized")
    resp = await client.request("ping")
    assert resp["result"] == {}, resp

    resp = await client.request("tools/list")
    tools = {t["name"]: t for t in resp["result"]["tools"]}
    assert set(tools) == {"validate", "list_gates", "baton_schema", "resume", "run"}, tools
    for t in tools.values():
        schema = t["inputSchema"]
        assert schema["type"] == "object" and "root_path" in schema["properties"], t
        assert "root_path" in schema["required"], t
        assert t["description"], t
    assert "baton_id" in tools["resume"]["inputSchema"]["required"]
    assert "decision" in tools["resume"]["inputSchema"]["required"]
    assert "baton_id" in tools["baton_schema"]["inputSchema"]["required"]


async def scenario_validate(client: _Client) -> None:
    d = tempfile.mkdtemp()
    good = _write_project(d, LINEAR_PIPELINE)
    is_err, out = await _call(client, "validate", {"root_path": good})
    assert not is_err and out["ok"] and out["errors"] == [], out

    # an INVALID config is a SUCCESSFUL validation: ok:false, isError false,
    # did-you-mean survives into the diagnostic
    bad = os.path.join(d, "bad.json")
    json.dump({"pipeline": "pipeline.json", "transprt": {"type": "inproc"}}, open(bad, "w"))
    is_err, out = await _call(client, "validate", {"root_path": bad})
    assert not is_err and not out["ok"], out
    assert any("transprt" in e["message"] and "transport" in e["message"]
               for e in out["errors"]), out

    # a stage-scoped pipeline error carries the stage field
    bad2 = os.path.join(d, "bad2.json")
    broken = {"nodes": LINEAR_PIPELINE["nodes"],
              "graph": {"start": "write", "stages": {
                  "write": {"node": "role:writer", "on_error": "claer", "then": None}}}}
    json.dump(broken, open(os.path.join(d, "p2.json"), "w"))
    json.dump({"pipeline": "p2.json"}, open(bad2, "w"))
    is_err, out = await _call(client, "validate", {"root_path": bad2})
    assert not is_err and not out["ok"], out
    assert out["errors"][0].get("stage") == "write", out

    # a MISSING file is a tool error (there was nothing to validate)
    is_err, text = await _call(client, "validate", {"root_path": os.path.join(d, "nope.json")})
    assert is_err and "nope.json" in text, text


async def scenario_run_linear(client: _Client) -> None:
    d = tempfile.mkdtemp()
    root = _write_project(d, LINEAR_PIPELINE)
    with open(os.path.join(d, "mcp_noise_transform.py"), "w") as f:
        f.write(NOISE_TRANSFORM)
    is_err, out = await _call(client, "run", {"root_path": root})
    assert not is_err, out
    assert out["outcome"] == "done", out
    assert "thinking" in out["payload"]["raw"], out  # the fake writer's text
    assert out["payload"]["note"] == {"ok": True}, out  # the noisy transform ran


async def scenario_gate_roundtrip(client: _Client) -> None:
    """run parks at the human gate (file-backed store) -> list_gates shows it ->
    baton_schema surfaces the decision form -> resume approves -> done -> the
    mailbox is empty. Each tool call assembles a FRESH harness over the same
    store — the cross-process rendezvous, exercised in one process."""
    d = tempfile.mkdtemp()
    root = _write_project(d, GATED_PIPELINE,
                          {"state": {"type": "file", "dir": "state"}})

    is_err, out = await _call(client, "run", {"root_path": root})
    assert not is_err and out["outcome"] == "suspended", out
    assert out["awaiting"] == "spec:approve", out
    baton_id = out["baton_id"]
    assert "Approve this spec?" in out["ask"] and "thinking" in out["ask"], out

    is_err, out = await _call(client, "list_gates", {"root_path": root})
    assert not is_err, out
    assert [b["id"] for b in out["batons"]] == [baton_id], out
    assert out["batons"][0]["awaiting"] == "spec:approve", out
    assert "thinking" in out["batons"][0]["question"], out

    is_err, out = await _call(client, "baton_schema", {"root_path": root, "baton_id": baton_id})
    assert not is_err, out
    assert out["form"] == "approve_or_revise" and out["baton_id"] == baton_id, out
    assert "decision" in out["schema"]["properties"], out

    # revise loops back through the writer and parks AGAIN (a fresh baton)
    is_err, out = await _call(client, "resume", {
        "root_path": root, "baton_id": baton_id,
        "decision": {"decision": "revise", "feedback": "tighten AC-2"}})
    assert not is_err and out["outcome"] == "suspended", out
    baton2 = out["baton_id"]

    is_err, out = await _call(client, "resume", {
        "root_path": root, "baton_id": baton2, "decision": {"decision": "approve"}})
    assert not is_err and out["outcome"] == "done", out

    is_err, out = await _call(client, "list_gates", {"root_path": root})
    assert not is_err and out["batons"] == [], out

    # a spent baton is single-shot: resuming it again is a TOOL error, not a crash
    is_err, text = await _call(client, "resume", {
        "root_path": root, "baton_id": baton2, "decision": {"decision": "approve"}})
    assert is_err and baton2 in text, text

    # baton_schema on a nonexistent baton: tool error with the id in the message
    is_err, text = await _call(client, "baton_schema",
                               {"root_path": root, "baton_id": "nope"})
    assert is_err and "nope" in text, text


async def scenario_protocol_errors(client: _Client) -> None:
    resp = await client.request("bogus/method")
    assert resp["error"]["code"] == -32601, resp

    resp = await client.request("tools/call", {"name": "frobnicate", "arguments": {}})
    assert resp["error"]["code"] == -32602 and "frobnicate" in resp["error"]["message"], resp

    resp = await client.request("tools/call", {"name": "run", "arguments": {}})
    assert resp["error"]["code"] == -32602 and "root_path" in resp["error"]["message"], resp

    # malformed line -> -32700 with id null, and the session SURVIVES
    client.send_line("{this is not json")
    resp = await client.recv()
    assert resp["error"]["code"] == -32700 and resp["id"] is None, resp
    resp = await client.request("ping")
    assert resp["result"] == {}, resp


def scenario_process_stdio_smoke() -> None:
    """serve_process_stdio over REAL pipes — the production wiring the CLI's
    `mcp-serve` action will call. One initialize + one ping over a spawned
    process's stdio, then EOF ends the session with exit 0."""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import asyncio; from yaah.adapters.mcp_server import serve_process_stdio; "
         "asyncio.run(serve_process_stdio())"],
        input=(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2024-11-05",
                                      "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "0"}}}) + "\n"
               + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"),
        capture_output=True, text=True, timeout=60,
        env=dict(os.environ, PYTHONPATH=SRC))
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    lines = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    assert len(lines) == 2, proc.stdout
    assert lines[0]["id"] == 1 and lines[0]["result"]["serverInfo"]["name"] == "yaah", lines
    assert lines[1]["id"] == 2 and lines[1]["result"] == {}, lines


async def amain() -> None:
    client = _Client()
    client.start()
    await scenario_handshake_and_listing(client)
    await scenario_validate(client)

    # stdout hygiene: in stdio mode stdout IS the protocol channel, so NOTHING
    # may leak onto it while tools run. The default console trace sink goes to
    # stderr (fine — MCP's logging channel), but a user `fn:` transform printing
    # to stdout (NOISE_TRANSFORM above) is real noise the server must swallow.
    # Falsifier: remove the server's redirect_stdout and this assertion fails.
    leak = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = leak
    try:
        await scenario_run_linear(client)
        await scenario_gate_roundtrip(client)
    finally:
        sys.stdout = real_stdout
    assert leak.getvalue() == "", "engine output leaked onto the protocol channel:\n" + leak.getvalue()

    await scenario_protocol_errors(client)
    await client.close()


def main() -> None:
    asyncio.run(amain())
    scenario_process_stdio_smoke()
    print("ok")


if __name__ == "__main__":
    main()
