"""Y5 — schema-gated recovery wired into extract_json(text, keys, schema).

The strong weak-executor backstop: when the normal pipeline AND Y4 key-recovery both
fail, a declared schema lets extract_json pluck bare/unquoted VALUES (enum members,
type:string free-form) and the one-`key: value`-per-line shape. It is ANCHORED on the
required keys (no required key -> no recovery) and ALL-OR-NOTHING (a missing required
key -> raise, never a half object). schema=None is byte-identical to the legacy path.

Run: cd yaah && PYTHONPATH=src python3 tests/test_jsonio_schema.py
"""
from __future__ import annotations

import json

from yaah.jsonio import extract_json

SCH = {
    "properties": {
        "verdict": {"enum": ["FIX", "SKIP", "ESCALATE"]},
        "severity": {"enum": ["high", "medium", "low"]},
        "confidence": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "severity", "confidence", "reason"],
}
REQ = SCH["required"]


def _raises(text, **kw):
    try:
        extract_json(text, **kw)
        return False
    except json.JSONDecodeError:
        return True


def recovers_line_protocol() -> None:
    raw = ("verdict: FIX\nseverity: high\nconfidence: 100\n"
           "reason: SQL injection — user_id concatenated into the query")
    assert extract_json(raw, keys=REQ, schema=SCH) == {
        "verdict": "FIX", "severity": "high", "confidence": 100,
        "reason": "SQL injection — user_id concatenated into the query"}


def recovers_bare_and_unquoted_values() -> None:
    # unquoted key AND bare enum/number value AND quoted free-form — Y4 alone can't
    raw = '{verdict: FIX, severity: high, confidence: 90, reason: "hardcoded token"}'
    assert extract_json(raw, keys=REQ, schema=SCH) == {
        "verdict": "FIX", "severity": "high", "confidence": 90, "reason": "hardcoded token"}


def anchored_empty_required_no_recovery() -> None:
    # no required key to anchor/verify against -> no Y5 recovery (same contract as Y4)
    assert _raises('verdict: FIX\nseverity: high',
                   keys=[], schema={"properties": SCH["properties"], "required": []})
    assert _raises('verdict: FIX\nseverity: high', schema=SCH)  # keys omitted entirely


def all_or_nothing_missing_required() -> None:
    # confidence + reason omitted -> recovery must raise, never a half object
    assert _raises('verdict: FIX\nseverity: high', keys=REQ, schema=SCH)


def never_fabricates_off_contract() -> None:
    assert _raises('verdict: MAYBE\nseverity: high\nconfidence: 9\nreason: x',
                   keys=REQ, schema=SCH)               # MAYBE not in enum
    assert _raises('verdict: FIX\nseverity: high\nconfidence: lots\nreason: x',
                   keys=REQ, schema=SCH)               # confidence not an integer


def schema_none_is_legacy() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}
    assert _raises('{verdict: FIX}')                   # unquoted, no keys/schema -> fails
    # Y4 still works without a schema (unquoted KEY, quoted value)
    assert extract_json('{verdict:"FIX"}', keys=["verdict"]) == {"verdict": "FIX"}


def wellformed_json_never_reaches_recovery() -> None:
    # a clean object parses at tier 1; schema/keys are irrelevant
    raw = '{"verdict":"SKIP","severity":"low","confidence":5,"reason":"ok"}'
    assert extract_json(raw, keys=REQ, schema=SCH)["verdict"] == "SKIP"


def main() -> None:
    recovers_line_protocol()
    recovers_bare_and_unquoted_values()
    anchored_empty_required_no_recovery()
    all_or_nothing_missing_required()
    never_fabricates_off_contract()
    schema_none_is_legacy()
    wellformed_json_never_reaches_recovery()
    print("ok")


if __name__ == "__main__":
    main()
