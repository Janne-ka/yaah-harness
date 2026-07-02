"""ToolsContributor — the tool-call capture.

Used by: a Tracer's contributor set when `tools` is enabled (`capture: [phase,
tools]`); pairs with the tool_call spans run_tool_loop emits.
Where: the engine's bundled contributors (pure projection).
Why: records which model-initiated tools fired (name + outcome) so a run's tool
usage is visible — orthogonal to cost, so a deployment can trace tools without
token detail or vice versa.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict

from ..contributor import TraceContributor
from ..span import Span


class ToolsContributor(TraceContributor):
    name = "tools"

    def contribute(self, span: Span) -> Dict[str, Any]:
        if span.name != "tool_call":
            return {}
        return {"tool": span.tool}
