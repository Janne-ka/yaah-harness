"""HumanGate — a node that suspends the run for a human decision.

Used by: the 'human_gate' node type (built by builders); the harness parks the
baton when it returns an `await` envelope, and resume() delivers the decision.
Where: approval/gate stages (spec-review, data-audit, ...).
Why: realize human-in-the-loop on top of the suspend/resume primitive, as a node.

Targets Python 3.9+.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

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
    def __init__(self, ask: str = "", awaiting: Optional[str] = None,
                 form: Optional[str] = None,
                 decision_schema: Optional[Dict[str, Any]] = None) -> None:
        # `form` names a generic decision shape from harness.decision_forms
        # (approve, approve_or_revise, free_text, json_schema). It rides on the
        # AWAIT envelope so `yaah baton-schema` can surface the matching schema
        # to a driver skill without re-loading the pipeline. `decision_schema`
        # is the inline schema for form == "json_schema" (escape hatch).
        # The builder validates the combo at LOAD time.
        self._ask = ask
        self._awaiting = awaiting
        self._form = form
        self._decision_schema = decision_schema

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        # Render the question against the parked artifact (e.g. "{{spec}}") so the
        # mailbox view shows the human exactly what they're deciding on. The harness
        # augments the parked artifact with this reply (see Harness._produce_single).
        ask = _fill(self._ask, input.payload)
        extra: Dict[str, Any] = {}
        if self._form is not None:
            extra["form"] = self._form
        if self._decision_schema is not None:
            extra["decision_schema"] = self._decision_schema
        return input.reply(Kind.AWAIT, ask=ask,
                           awaiting=self._awaiting or "human", **extra)
