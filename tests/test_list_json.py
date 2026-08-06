"""The `yaah list --json` mailbox shape.

What it proves: the stable JSON shape `{id, stage, awaiting, parked_at,
checkpointed_at, concerns, escalation, question}` plus the ADDITIVE recovery
fields `{owner, leased_at, lease_state, wiring, wiring_mismatch}` the CLI emits for
each baton — the contract a driver skill consumes instead of parsing the prose
`GATE …` lines. Covers: question lifted from `payload['question']` OR
`payload['ask']`, null when neither is present, full concerns list passes through,
the lease label appearing only when a root supplies the horizon, and
`wiring_mismatch` staying NULL (not false) when there is nothing to compare.
Usability-gaps §5 (skill interface).

Run: cd yaah && PYTHONPATH=src python3 tests/test_list_json.py

Targets Python 3.9+.
"""
from __future__ import annotations

from yaah.core import Envelope, Kind
from yaah.harness.baton import Baton
from yaah.runtime import _baton_json


def main() -> None:
    # gate that asked an explicit question (parked_at carried through)
    b1 = Baton(id="b-1", stage="review", awaiting="human:approve_or_revise",
               status="suspended", parked_at=1769990400.0,
               concerns=[{"by": "schema", "msg": "missing key"}],
               pending=Envelope(Kind.AWAIT, {"question": "ship it?"}))
    j1 = _baton_json(b1)
    assert j1 == {"id": "b-1", "stage": "review",
                  "awaiting": "human:approve_or_revise",
                  "parked_at": 1769990400.0,
                  "checkpointed_at": None,
                  "concerns": [{"by": "schema", "msg": "missing key"}],
                  "escalation": None,
                  "question": "ship it?",
                  "owner": None, "leased_at": None, "lease_state": None,
                  "wiring": None, "wiring_mismatch": None}, j1

    # gate that used `ask` instead of `question` (HumanGate's default key)
    b2 = Baton(id="b-2", stage="audit", awaiting="human:data-audit",
               status="suspended", concerns=[],
               pending=Envelope(Kind.AWAIT, {"ask": "approve the audit?"}))
    j2 = _baton_json(b2)
    assert j2["question"] == "approve the audit?", j2

    # gate with no question/ask payload — question is null, not missing
    b3 = Baton(id="b-3", stage="sleep", awaiting="external",
               status="suspended", concerns=[],
               pending=Envelope(Kind.AWAIT, {"some": "other"}))
    j3 = _baton_json(b3)
    assert j3["question"] is None and "question" in j3, j3

    # gate with no pending envelope at all — question is null; a baton that never
    # parked has parked_at null (not missing), so a disambiguating driver can tell
    # "unknown park time" from an old timestamp
    b4 = Baton(id="b-4", stage=None, awaiting=None, status="suspended",
               concerns=[], pending=None)
    j4 = _baton_json(b4)
    assert j4 == {"id": "b-4", "stage": None, "awaiting": None,
                  "parked_at": None, "checkpointed_at": None,
                  "concerns": [], "escalation": None, "question": None,
                  "owner": None, "leased_at": None, "lease_state": None,
                  "wiring": None, "wiring_mismatch": None}, j4
    assert "parked_at" in j4, j4

    # A running checkpoint (Level 2): no parked_at, a checkpointed_at wall-clock —
    # the recovery view's disambiguation key.
    b6 = Baton(id="b-6", stage="code", status="running",
               cursor_input=Envelope(Kind.RESULT, {"steps": ["red"]}),
               checkpointed_at=1769990500.0)
    j6 = _baton_json(b6)
    assert j6["parked_at"] is None and j6["checkpointed_at"] == 1769990500.0, j6

    # gate that escalated after exhausting attempts — the failed verdict folded
    # onto the parked payload (Y3) surfaces under `escalation`
    b5 = Baton(id="b-5", stage="audit", awaiting="human:audit",
               status="suspended", concerns=[],
               pending=Envelope(Kind.AWAIT, {"escalation": {
                   "stage": "audit",
                   "failures": [{"code": "not_ok", "message": "needs ok=true",
                                 "fix_hint": "set ok=true"}]}}))
    j5 = _baton_json(b5)
    assert j5["escalation"]["failures"][0]["code"] == "not_ok", j5

    # A LEASED running checkpoint: `lease_state` is computed only when a root is
    # supplied (the horizon is a root fact), and `wiring_mismatch` compares the
    # baton's stamp against the current graph's fingerprint the caller passes.
    b7 = Baton(id="b-7", stage="code", status="running",
               cursor_input=Envelope(Kind.RESULT, {}),
               checkpointed_at=1769990500.0,
               owner="otherhost/4242/abcd1234", leased_at=1769990500.0,
               wiring="aaaa")
    assert _baton_json(b7)["lease_state"] is None, "no root -> no lease verdict"
    j7 = _baton_json(b7, {"lease_horizon": 3600}, "bbbb")
    assert j7["owner"] == "otherhost/4242/abcd1234", j7
    assert j7["leased_at"] == 1769990500.0, j7
    assert j7["lease_state"] in ("stale", "foreign"), j7   # foreign host, age-dependent
    assert j7["wiring"] == "aaaa" and j7["wiring_mismatch"] is True, j7
    assert _baton_json(b7, {}, "aaaa")["wiring_mismatch"] is False, "same wiring"
    # NOT compared is null, never false — "unknown" must not read as "matches"
    assert _baton_json(b7, {})["wiring_mismatch"] is None, "no current fingerprint"
    assert _baton_json(b6, {}, "aaaa")["wiring_mismatch"] is None, "pre-upgrade baton"

    # the contract is the keyset itself — a skill iterating fields must not be
    # surprised by drift
    assert set(j1.keys()) == {"id", "stage", "awaiting", "parked_at",
                              "checkpointed_at", "concerns", "escalation",
                              "question", "owner", "leased_at", "lease_state",
                              "wiring", "wiring_mismatch"}

    print("PASS yaah list --json shape: {id, stage, awaiting, parked_at, "
          "checkpointed_at, concerns, escalation, question, owner, leased_at, "
          "lease_state, wiring, wiring_mismatch}")


if __name__ == "__main__":
    main()
