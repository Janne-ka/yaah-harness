"""Catalog of generic decision shapes a HumanGate can declare it expects.

Used by: the `human_gate` builder (validates the `form` config field), the
`yaah baton-schema` CLI (returns the matching schema so a driver skill can
compose a decision mechanically), and the runtime when an inline `json_schema`
form is in play.
Where: imported by `build/builders.py::_build_human_gate` and
`runtime.py::_dispatch` for the baton-schema action. The HumanGate node itself
imports the catalog only to validate; the schemas are surfaced via the CLI.
Why: gives Claude Code skills and other driver tools a CROSS-CUTTING vocabulary
for parked gates. A skill that knows `approve_or_revise` can render a two-button
prompt for any yaah pipeline; without a shared vocabulary, every consumer
reinvents its own decision shape and the skill is back to per-pipeline prose
interpretation. The catalog stays small and curated — domain-specific shapes
use the `json_schema` form with an inline `decision_schema`, which is the
escape hatch.

Engine boundary: yaah owns the catalog (because cross-cutting value depends on
a stable shared vocabulary). yaah does NOT own app-specific decision payloads —
those are pipeline-author contract and ride via the `json_schema` form. See
`docs/decision-forms.md` for the extension story (when a new generic form is
worth adding to the catalog vs. when to use the inline escape hatch).

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


# The catalog. Adding an entry is a PR-level change (it becomes a stable public
# vocabulary tag — see docs/decision-forms.md "Extending the catalog"). The
# schemas use the JSON-Schema SUBSET yaah's own validator understands (type,
# enum, required, properties); `additionalProperties: false` is included for
# the benefit of consumer skills using a full JSON Schema validator, even though
# yaah's subset checker (validators._check_schema) does not enforce it.
FORMS: Dict[str, Dict[str, Any]] = {
    "approve": {
        "schema": {
            "type": "object",
            "properties": {"decision": {"enum": ["approve"]}},
            "required": ["decision"],
            "additionalProperties": False,
        },
        "example": {"decision": "approve"},
    },
    "approve_or_revise": {
        "schema": {
            "type": "object",
            "properties": {
                "decision": {"enum": ["approve", "revise"]},
                "feedback": {"type": "string"},
            },
            "required": ["decision"],
            "additionalProperties": False,
        },
        "example": {"decision": "approve"},
    },
    "free_text": {
        "schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        "example": {"answer": ""},
    },
    # Escape hatch: schema comes from the gate's inline `decision_schema`. Use
    # for gates whose payload shape doesn't fit any catalog entry and won't
    # recur across consumers (the test for "should this be a catalog form?" is
    # whether ≥3 unrelated pipelines need the same shape).
    "json_schema": {"schema": None, "example": {}},
}


def lookup(form: str, inline_schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return `{form, schema, example}` for a HumanGate's declared form. For
    `form == "json_schema"`, `inline_schema` IS the schema (caller's
    responsibility to have it). Raises ValueError on either an unknown form or
    a missing inline schema for `json_schema` — both are config bugs the
    builder should have caught."""
    if form not in FORMS:
        raise ValueError("unknown decision form {!r}; known: {}".format(
            form, sorted(FORMS)))
    entry = FORMS[form]
    schema = inline_schema if form == "json_schema" else entry["schema"]
    if schema is None:
        raise ValueError(
            "form 'json_schema' requires an inline `decision_schema` on the gate")
    return {"form": form, "schema": schema, "example": entry["example"]}
