"""HumanGate `form` / `decision_schema` wiring — builder + node.

What it proves: the builder rejects (a) unknown form names, (b) form='json_schema'
without inline decision_schema, (c) decision_schema with a non-json_schema form;
a built HumanGate that declared a form emits it on the AWAIT envelope so the
harness can park it on baton.pending.payload for `yaah baton-schema` to surface.
Defends the contract decision-forms.md describes.

Run: cd yaah && PYTHONPATH=src python3 tests/test_human_gate_form.py

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio

from yaah.build.builders import _build_human_gate
from yaah.core import Envelope, Kind


def _build(spec: dict):
    # _build_human_gate doesn't touch ctx in any branch — pass None to skip
    # the comms/backend setup a full BuildContext would require.
    return _build_human_gate(spec, ctx=None)


def _await_payload(gate, payload: dict) -> dict:
    """Drive a HumanGate once and return the AWAIT envelope's payload."""
    inp = Envelope(Kind.TASK, payload)
    out = asyncio.run(gate.invoke(inp, {}))
    assert out.kind == Kind.AWAIT, out.kind
    return out.payload


def main() -> None:
    # happy path: built-in form rides on the AWAIT envelope
    g = _build({"ask": "ship?", "awaiting": "human:ship",
                "form": "approve_or_revise"})
    p = _await_payload(g, {})
    assert p["form"] == "approve_or_revise", p
    assert "decision_schema" not in p
    assert p["awaiting"] == "human:ship"

    # json_schema escape hatch: inline schema rides too
    inline = {"type": "object", "properties": {"verdict": {"type": "string"}},
              "required": ["verdict"]}
    g2 = _build({"ask": "?", "awaiting": "human:grill",
                 "form": "json_schema", "decision_schema": inline})
    p2 = _await_payload(g2, {})
    assert p2["form"] == "json_schema" and p2["decision_schema"] == inline, p2

    # no form declared: backward compatible — neither key on the AWAIT envelope
    g3 = _build({"ask": "legacy", "awaiting": "human"})
    p3 = _await_payload(g3, {})
    assert "form" not in p3 and "decision_schema" not in p3, p3

    # rejection cases — every one of these is a config bug, caught at build
    bad_cases = [
        ({"form": "bogus"}, "not a known decision form"),
        ({"form": "json_schema"}, "requires an inline 'decision_schema'"),
        ({"form": "approve", "decision_schema": {"type": "object"}},
         "only allowed when form == 'json_schema'"),
    ]
    for spec, needle in bad_cases:
        try:
            _build(spec)
        except ValueError as e:
            assert needle in str(e), (spec, needle, str(e))
        else:
            raise AssertionError("expected ValueError for {!r}".format(spec))

    print("PASS HumanGate form/decision_schema: builder validates, AWAIT carries them")


if __name__ == "__main__":
    main()
