"""The `yaah list --json` mailbox shape.

What it proves: the stable JSON shape `{id, stage, awaiting, concerns, question}`
the CLI emits for each suspended baton — the contract a driver skill consumes
instead of parsing the prose `GATE …` lines. Covers: question lifted from
`payload['question']` OR `payload['ask']`, null when neither is present, full
concerns list passes through. Usability-gaps §5 (skill interface).

Run: cd yaah && PYTHONPATH=src python3 tests/test_list_json.py

Targets Python 3.9+.
"""
from __future__ import annotations

from yaah.core import Envelope, Kind
from yaah.harness.baton import Baton
from yaah.runtime import _baton_json


def main() -> None:
    # gate that asked an explicit question
    b1 = Baton(id="b-1", stage="review", awaiting="human:approve_or_revise",
               status="suspended",
               concerns=[{"by": "schema", "msg": "missing key"}],
               pending=Envelope(Kind.AWAIT, {"question": "ship it?"}))
    j1 = _baton_json(b1)
    assert j1 == {"id": "b-1", "stage": "review",
                  "awaiting": "human:approve_or_revise",
                  "concerns": [{"by": "schema", "msg": "missing key"}],
                  "question": "ship it?"}, j1

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

    # gate with no pending envelope at all — question is null
    b4 = Baton(id="b-4", stage=None, awaiting=None, status="suspended",
               concerns=[], pending=None)
    j4 = _baton_json(b4)
    assert j4 == {"id": "b-4", "stage": None, "awaiting": None,
                  "concerns": [], "question": None}, j4

    # the contract is the keyset itself — a skill iterating fields must not be
    # surprised by drift
    assert set(j1.keys()) == {"id", "stage", "awaiting", "concerns", "question"}

    print("PASS yaah list --json shape: {id, stage, awaiting, concerns, question}")


if __name__ == "__main__":
    main()
