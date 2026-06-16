"""HumanGate — a node that suspends the run for a human decision.

Used by: the 'human_gate' node type (built by builders); the harness parks the
baton when it returns an `await` envelope, and resume() delivers the decision.
Where: approval/gate stages (spec-review, data-audit, ...).
Why: realize human-in-the-loop on top of the suspend/resume primitive, as a node.

Targets Python 3.9+.
"""
from __future__ import annotations

import re
from typing import Optional

from ..core import Envelope, Kind, NodeConfig

_PLACEHOLDER = re.compile(r"{{\s*(\w+)\s*}}")


def _fill(template: str, payload: dict) -> str:
    """Mustache-style {{key}} substitution from the payload (template from trusted
    config, not the payload). Unknown placeholders are left untouched. Mirrors the
    RenderNode/Agent template style — dependency-free and identical to read."""
    def sub(m: "re.Match") -> str:
        k = m.group(1)
        if k not in payload:
            return m.group(0)
        v = payload[k]
        return v if isinstance(v, str) else str(v)

    return _PLACEHOLDER.sub(sub, template)


class HumanGate:
    def __init__(self, ask: str = "", awaiting: Optional[str] = None) -> None:
        self._ask = ask
        self._awaiting = awaiting

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        # Render the question against the parked artifact (e.g. "{{spec}}") so the
        # mailbox view shows the human exactly what they're deciding on. The harness
        # augments the parked artifact with this reply (see Harness._produce_single).
        ask = _fill(self._ask, input.payload)
        return input.reply(Kind.AWAIT, ask=ask, awaiting=self._awaiting or "human")
