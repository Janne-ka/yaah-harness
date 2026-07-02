"""HardenedParser — the orchestrator extract_json delegates to.

Covers the behaviours the orchestrator ADDS over the flat tier list: the ambiguity
guard (refuse to silently pick between a format-example and the answer), the decoy
filter (drop a marked example so the answer wins), the truncation signal, and the
injectability of the pipeline. Faithful-refactor cases (clean/fenced/prose JSON,
single-quote recovery, Y4/Y5) live in test_jsonio*.py.

Run: cd yaah && PYTHONPATH=src python3 tests/test_hardened_parser.py
"""
from __future__ import annotations

import json

from yaah.jsonio import (
    DecoyKeyDetector,
    HardenedParser,
    PureJson,
    extract_json,
)


def _raises(fn):
    try:
        fn()
        return False
    except json.JSONDecodeError:
        return True


def ambiguity_guard_refuses_to_guess() -> None:
    # a format example beside the answer, both holding the required key -> RAISE,
    # not the old silent "first span wins" (which returned the example's value)
    assert _raises(lambda: extract_json(
        'format: {"verdict":"PASS"} answer: {"verdict":"FAIL"}', keys=["verdict"]))
    # two identical objects are NOT ambiguous (same value -> no wrong guess possible)
    assert extract_json('{"v":"A"} and {"v":"A"}', keys=["v"]) == {"v": "A"}


def single_qualifier_is_preferred() -> None:
    # first parse lacks the key, a later object has it -> return the qualifying one
    assert extract_json('{"x":1} then {"verdict":"FIX"}', keys=["verdict"]) == {
        "verdict": "FIX"}


def decoy_example_is_filtered_so_answer_wins() -> None:
    # the example marks its keys (n_o_..._o_n) -> dropped -> only the answer qualifies
    assert extract_json(
        'e.g. {"n_o_verdict_o_n":"PASS"} answer: {"verdict":"FAIL"}',
        keys=["verdict"]) == {"verdict": "FAIL"}


def no_keys_keeps_legacy_first_wins() -> None:
    # without keys there is nothing to anchor on -> first parseable object, as before
    assert extract_json('{"id":1}\n{"id":2}') == {"id": 1}
    assert extract_json('noise {"a":1} trailing') == {"a": 1}


def keys_none_multi_span_stays_fail_loud() -> None:
    # REGRESSION (eval 2026-06-29): with keys=None there is no anchor AND no ambiguity
    # guard, so a malformed first object followed by a valid one must RAISE (legacy
    # first-span behaviour) — a later valid object must not silently rescue it. The
    # unguarded json_object_validator path (extract_json(raw)) depends on this.
    assert _raises(lambda: extract_json('{not valid} {"a": 1}'))
    assert _raises(lambda: extract_json('prose {bad} then {"a":1} end'))
    assert _raises(lambda: extract_json('```\n{bad}\n```\n{"real":1}'))
    # but WITH keys, the guarded path does see the later span (improvement, not a leak)
    assert extract_json('{not valid} {"a": 1}', keys=["a"]) == {"a": 1}


def truncation_is_signalled_clearly() -> None:
    try:
        extract_json('{"findings":[{"title":"sql","detail":"the query at line')
        raise AssertionError("expected a JSONDecodeError")
    except json.JSONDecodeError as e:
        assert "truncated" in str(e).lower(), str(e)


def json_null_is_returned_not_treated_as_no_parse() -> None:
    # the _DEFER sentinel (not None) means "defer", so a valid JSON null survives
    assert extract_json("null") is None


def pipeline_is_injectable() -> None:
    # swap the strategy list: PureJson only -> a single-quote object no longer recovers
    strict = HardenedParser(strategies=[PureJson()])
    assert strict.parse('{"a":1}') == {"a": 1}
    assert _raises(lambda: strict.parse("{'a':1}"))   # KeyValueRepair not in the pipeline
    # swap the decoy marker
    custom = HardenedParser(decoy=DecoyKeyDetector(prefixes=("EX_",), suffixes=()))
    assert custom.parse('{"EX_v":"x"} answer: {"v":"y"}', keys=["v"]) == {"v": "y"}


def main() -> None:
    ambiguity_guard_refuses_to_guess()
    single_qualifier_is_preferred()
    decoy_example_is_filtered_so_answer_wins()
    no_keys_keeps_legacy_first_wins()
    keys_none_multi_span_stays_fail_loud()
    truncation_is_signalled_clearly()
    json_null_is_returned_not_treated_as_no_parse()
    pipeline_is_injectable()
    print("ok")


if __name__ == "__main__":
    main()
