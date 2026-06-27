"""Y4 — bounded key-guided recovery: extract_json(text, keys=...).

When the normal pipeline fails AND the caller supplies the node's declared output
keys, recover each key's scalar value by searching INSIDE the first balanced {...}
span only — never free prose (that would fabricate a value). All-or-nothing; a
missing key / no span / truncated input -> raise (safe, never mask). keys=None is
byte-identical to today.

Run: cd yaah && PYTHONPATH=src python3 tests/test_jsonio_keys.py
"""
from __future__ import annotations

import json

from yaah.jsonio import extract_json


def _raises(text, keys):
    try:
        extract_json(text, keys=keys)
        return False
    except json.JSONDecodeError:
        return True


def recovers_unquoted_keys() -> None:
    # the BUG-697 Instance 3 shape: unquoted keys defeat json.loads + literal_eval
    assert extract_json('{verdict:"FIX"}', keys=["verdict"]) == {"verdict": "FIX"}
    assert extract_json('{verdict:"FIX", confidence:"high"}',
                        keys=["verdict", "confidence"]) == {"verdict": "FIX", "confidence": "high"}


def bounds_to_span_no_prose_masking() -> None:
    # the disqualifying case the eval PROVED: a key named in prose must NOT win —
    # recovery happens only inside the balanced {...} span, so the real value is taken.
    assert extract_json('Reasoning: my verdict: leaning FIX. Final: {verdict:"FIX"}',
                        keys=["verdict"]) == {"verdict": "FIX"}
    assert extract_json('Here is what verdict: means. {verdict:"SKIP"}',
                        keys=["verdict"]) == {"verdict": "SKIP"}


def fails_safe_never_fabricates() -> None:
    # truncated/unbalanced -> no span; pure prose -> no span; empty -> raise. Never a half value.
    assert _raises('{verdict:"FI', ["verdict"])      # truncated
    assert _raises('verdict: FIX', ["verdict"])      # prose, no object
    assert _raises('', ["verdict"])                  # empty


def all_or_nothing() -> None:
    # a required key absent from the object -> raise, not a partial dict
    assert _raises('{verdict:"FIX"}', ["verdict", "why"])


def no_masking_from_string_value() -> None:
    # a key NAME appearing inside ANOTHER key's quoted value must NOT be grabbed
    # (the regex masking bug the verifier found). Must read the real top-level key.
    assert extract_json('{note:"verdict: SKIP", verdict:"FIX"}',
                        keys=["verdict"])["verdict"] == "FIX"
    assert extract_json('{note:"the verdict: is bad", verdict:"FIX"}',
                        keys=["verdict"])["verdict"] == "FIX"


def no_masking_from_nested_object() -> None:
    # a nested {verdict:...} must not mask the real top-level verdict
    assert extract_json('{outer:{verdict:"SKIP"}, verdict:"FIX"}',
                        keys=["verdict"])["verdict"] == "FIX"


def key_only_nested_or_in_string_fails_safe() -> None:
    # the key exists ONLY inside a nested object / a string value, never as a real
    # top-level key -> must RAISE, never fabricate a value from it.
    assert _raises('{outer:{verdict:"SKIP"}}', ["verdict"])
    assert _raises('{note:"verdict: SKIP"}', ["verdict"])


def value_with_delimiters_inside_string() -> None:
    # commas/colons inside a quoted value must not truncate or corrupt it
    assert extract_json('{verdict:"a, b: c", n:1}', keys=["verdict", "n"]) \
        == {"verdict": "a, b: c", "n": 1}


def no_masking_from_array_element_object() -> None:
    # the most plausible real-LLM masking vector: the key inside an array-of-objects
    # must not mask the real top-level key, and a key found ONLY in an array element
    # must fail safe (not fabricate).
    assert extract_json('{items:[{verdict:"X"}], verdict:"FIX"}',
                        keys=["verdict"])["verdict"] == "FIX"
    assert _raises('{items:[{verdict:"X"}]}', ["verdict"])


def keys_none_is_unchanged() -> None:
    assert extract_json('{"a":1}') == {"a": 1}
    assert extract_json('{"a":1}', keys=None) == {"a": 1}
    assert _raises('{a:1}', None)  # unquoted, no keys -> still fails (no recovery path)


def normal_parse_wins_over_recovery() -> None:
    # well-formed JSON parses normally even when keys are supplied (recovery not reached)
    assert extract_json('{"verdict":"FIX"}', keys=["verdict"]) == {"verdict": "FIX"}


def quoted_value_and_number() -> None:
    # unquoted KEYS recover; values must be quoted/number/bool/null (an unquoted
    # bare-word string value is an unsafe guess -> not supported, fails safe).
    assert extract_json('{verdict:"FIX", count:5}', keys=["verdict", "count"]) \
        == {"verdict": "FIX", "count": 5}


def main() -> None:
    recovers_unquoted_keys()
    bounds_to_span_no_prose_masking()
    fails_safe_never_fabricates()
    all_or_nothing()
    no_masking_from_string_value()
    no_masking_from_nested_object()
    key_only_nested_or_in_string_fails_safe()
    value_with_delimiters_inside_string()
    no_masking_from_array_element_object()
    keys_none_is_unchanged()
    normal_parse_wins_over_recovery()
    quoted_value_and_number()
    print("ok")


if __name__ == "__main__":
    main()
