"""render_tool_manifest — the R11 prompt-side advertisement for an agent's tools.

Used by: Agent._render — when an agent has `tools` and the backend lacks
function-calling (`turn`), the manifest is rendered into the prompt via the
`{{tool_manifest}}` placeholder so the model knows what's available and how to
call it. With a turn-capable backend (litellm), the SAME Tool spec is consumed
via `to_function_schema()` instead, so descriptions/schemas live in ONE place.
Where: a pure function (no I/O, no config) — same line as jsonio/recall.
Why: hand-written prompts tend to inline tool usage instructions, down to
absolute script paths (`bash /Users/<you>/scripts/fetch-changed.sh`). That
breaks portability and forces a prompt edit for every tool change. The
manifest renders ONCE from the Tool spec; prompts just reference it.

Manifest format (Markdown, deterministic so prompts can be diffed):

    ## Tools you can call
    - **{name}** — {description}
      Args (JSON Schema): {schema}
      How to call: {usage or default-invocation-line}

If a tool has a `usage` field, it's rendered verbatim (the author's exact
instruction). Otherwise a generic fallback is used.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
from typing import Iterable

from .tool import Tool


def render_tool_manifest(tools: Iterable[Tool]) -> str:
    """Return the Markdown manifest for `tools`. Empty input → empty string
    (so `{{tool_manifest}}` cleanly vanishes from a prompt that has no tools).

    Closure tools (a callable `impl` — per-invocation handlers like envelope_get)
    are rendered ONLY when the author supplied an explicit `usage` line
    (assessment #10): the generic `output '{"tool_call"...}'` fallback is a lie
    for them — nothing on the prompt-side path ever parses that line back, so
    advertising the tool invites the model to call into a void. A call_target
    string impl keeps the fallback (the author can wire a parser downstream)."""
    lst = [t for t in tools
           if t is not None and (not callable(t.impl) or getattr(t, "usage", ""))]
    if not lst:
        return ""
    parts = ["## Tools you can call", ""]
    for t in lst:
        parts.append("- **{}** — {}".format(t.name, t.description or "(no description)"))
        if t.schema:
            parts.append("  Args (JSON Schema): `{}`".format(
                json.dumps(t.schema, separators=(",", ":"))))
        usage = getattr(t, "usage", "") or _default_usage(t)
        parts.append("  How to call: {}".format(usage))
    return "\n".join(parts) + "\n"


def _default_usage(tool: Tool) -> str:
    """Fallback invocation hint for a tool with no explicit `usage`. Uses the
    name and schema so a model still has something concrete to do. Native-tool
    delivery (Bash/Read/MCP) is what the author would override `usage` for."""
    return "output `{{\"tool_call\": \"{}\", \"args\": {{...}}}}` on its own line".format(
        tool.name)
