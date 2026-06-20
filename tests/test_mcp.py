"""Agent MCP config: the pluggable McpSource ('agentMcpGet') + claude wiring.

MCP is model-initiated agent config (not a node). An agent's `mcp` is either an
inline servers map or a 'source:key' ref fetched from an McpSource; the resolved
servers reach claude as --mcp-config.

Run: cd yaah && PYTHONPATH=src python3 tests/test_mcp.py
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

from yaah import Envelope, Kind, NodeConfig
from yaah.agents import Agent
from yaah.adapters.backends import ClaudeCliBackend
from yaah.mcp import RoutingMcpSource, StaticMcpSource, normalize_servers
from yaah.adapters.mcp import FileMcpSource

SERVERS = {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}


async def scenario_sources() -> None:
    # static, and both shapes normalize to the servers map
    static = StaticMcpSource({"prod": {"mcpServers": SERVERS}, "bare": SERVERS})
    assert await static.get("prod") == SERVERS
    assert await static.get("bare") == SERVERS
    assert normalize_servers({"mcpServers": SERVERS}) == SERVERS

    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "acme-prod.json"), "w") as f:
            json.dump({"mcpServers": SERVERS}, f)
        files = FileMcpSource(d)
        assert await files.get("acme-prod") == SERVERS

        routing = RoutingMcpSource({"file": files, "static": static}, default="static")
        assert await routing.get("file:acme-prod") == SERVERS
        assert await routing.get("prod") == SERVERS  # default source


class CaptureBackend:
    """Records the opts the agent passed (stands in for a real model)."""
    def __init__(self):
        self.opts = None

    async def complete(self, prompt, *, model=None, **opts):
        self.opts = opts
        return "ok"


async def scenario_agent_resolves_ref_and_inline() -> None:
    src = RoutingMcpSource({"registry": StaticMcpSource({"prod": SERVERS})}, default="registry")

    # a 'source:key' ref is fetched and handed to the backend as opts["mcp"]
    cap = CaptureBackend()
    a = Agent(cap, template="hi", mcp="registry:prod", mcp_source=src, parse=False)
    await a.invoke(Envelope(Kind.TASK, {}), NodeConfig())
    assert cap.opts["mcp"] == SERVERS, cap.opts

    # an inline servers map needs no source
    cap2 = CaptureBackend()
    a2 = Agent(cap2, template="hi", mcp={"mcpServers": SERVERS}, parse=False)
    await a2.invoke(Envelope(Kind.TASK, {}), NodeConfig())
    assert cap2.opts["mcp"] == SERVERS, cap2.opts


async def scenario_claude_mcp_args() -> None:
    """The resolved servers reach claude as --mcp-config (no claude spawn)."""
    backend = ClaudeCliBackend()
    args = backend._build_args("claude-sonnet-4-6", {"mcp": SERVERS,
                                                     "allowed_tools": ["mcp__fetch__fetch"]})
    joined = " ".join(args)
    assert "--mcp-config" in args
    cfg = json.loads(args[args.index("--mcp-config") + 1])
    assert cfg == {"mcpServers": SERVERS}, cfg
    assert "--strict-mcp-config" in args  # ours only, ignore project .mcp.json
    assert "mcp__fetch__fetch" in joined


async def main() -> None:
    await scenario_sources()
    await scenario_agent_resolves_ref_and_inline()
    await scenario_claude_mcp_args()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
