"""yaah.adapters.mcp_server — yaah's operator surface as an MCP *server*.

Who: an MCP-capable agent host (Claude Code, Cursor, any client speaking the
Model Context Protocol) that wants to operate yaah pipelines natively —
validate configs, run, list parked gates, read a gate's decision form, resume
with a decision — as structured tool calls instead of CLI string parsing.
Where: this package sits BESIDE `yaah.cli` as an operator entry surface (it is
an entry point like the CLI, not a port implementation — it calls the same
runtime assembly the CLI actions call). Transport is newline-delimited
JSON-RPC 2.0 over stdio, the MCP stdio framing.
Why: "AI-operable workflows" — the mailbox flow (`yaah list` → `baton-schema`
→ decision → `resume`) becomes five typed tools any agent host can drive.

Stdlib-only, like the engine core (zero new runtime deps).

Entry points:
  serve_stdio(reader, writer)  — testable core loop over any stream pair
  serve_process_stdio()        — wires the loop onto the process's real stdio
"""
from .server import McpServer, serve_process_stdio, serve_stdio
from .tools import TOOLS

__all__ = ["McpServer", "TOOLS", "serve_process_stdio", "serve_stdio"]
