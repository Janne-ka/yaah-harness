"""Hardened-parser helper classes (built step by step).

#1 DecoyKeyDetector — a format-example object in a prompt uses deliberately-wrong
keys with a recognizable affix (real key `verdict` -> decoy `n_o_verdict_o_n`), so
the parser can tell a format example apart from the model's real answer and skip it.

Run: cd yaah && PYTHONPATH=src python3 tests/test_parsers.py
"""
from __future__ import annotations

from yaah.jsonio import (
    BareValueResolver,
    DecoyKeyDetector,
    Scanner,
    UnquotedKeyValue,
)


def detects_decoy_keys_by_affix() -> None:
    d = DecoyKeyDetector()
    assert d.is_decoy("n_o_verdict_o_n")
    assert d.is_decoy("n_o_score_o_n")
    assert not d.is_decoy("verdict")
    assert not d.is_decoy("score")


def detects_decoy_objects() -> None:
    d = DecoyKeyDetector()
    assert d.is_decoy_object({"n_o_verdict_o_n": "PASS", "n_o_score_o_n": 0})
    assert not d.is_decoy_object({"verdict": "FAIL", "score": 7})
    assert not d.is_decoy_object({})          # empty isn't a decoy
    assert not d.is_decoy_object("not a dict")


def affixes_are_configurable_one_line() -> None:
    d = DecoyKeyDetector(prefixes=("EXAMPLE_",), suffixes=())
    assert d.is_decoy("EXAMPLE_verdict")
    assert not d.is_decoy("n_o_verdict_o_n")  # default marker no longer applies


# ── #2 Scanner — string- and bracket-aware primitive ────────────────────────

def balanced_spans_finds_top_level_objects() -> None:
    s = Scanner()
    # the #2 multi-object case: TWO top-level spans, not one
    assert s.balanced_spans('{"id":1}\n{"id":2}') == ['{"id":1}', '{"id":2}']
    # mixed bracket kinds, surrounded by prose
    assert s.balanced_spans('noise {"a":1} mid [1,2] end') == ['{"a":1}', '[1,2]']
    # no JSON at all
    assert s.balanced_spans('just prose') == []


def balanced_spans_respects_strings_and_nesting() -> None:
    s = Scanner()
    # a brace inside a string must not open/close depth
    assert s.balanced_spans('{"msg":"a } b { c"}') == ['{"msg":"a } b { c"}']
    # nested object is ONE top-level span
    assert s.balanced_spans('{"o":{"i":1}}') == ['{"o":{"i":1}}']
    # mixed nesting of all three bracket kinds, balanced
    assert s.balanced_spans('({[]})') == ['({[]})']


def match_returns_corresponding_close() -> None:
    s = Scanner()
    assert s.match('({[]})', 0) == 5          # '(' waits for the final ')'
    assert s.match('{"a":1}', 0) == 6
    assert s.match('{"x":"} not me"}', 0) == 15  # '}' inside string ignored; real close at 15
    assert s.match('{unterminated', 0) is None    # never closes -> None


def split_top_level_splits_only_at_depth_zero() -> None:
    s = Scanner()
    assert s.split_top_level('a,b,c', [',']) == ['a', 'b', 'c']
    # commas INSIDE brackets are not split points
    assert s.split_top_level('[1,2],[3,4]', [',']) == ['[1,2]', '[3,4]']
    # a separator inside a string is not a split point
    assert s.split_top_level('"a,b",c', [',']) == ['"a,b"', 'c']
    # newline as the separator (the #2 object boundary)
    assert s.split_top_level('{"id":1}\n{"id":2}', ['\n']) == ['{"id":1}', '{"id":2}']


def split_top_level_takes_multiple_separators() -> None:
    s = Scanner()
    # ':' and ' ' both separate — the key:value pluck the #3 plucker will lean on
    assert s.split_top_level('verdict: FIX', [':', ' ']) == ['verdict', 'FIX']
    # consecutive separators collapse (no empty parts)
    assert s.split_top_level('a, , b', [',', ' ']) == ['a', 'b']
    # separators default to the constructor's when omitted
    assert Scanner(separators=(',',)).split_top_level('x,y') == ['x', 'y']


# ── #3a BareValueResolver — bare value gated by the schema (enum or type) ────

_SCH = {"properties": {
    "verdict": {"enum": ["FIX", "SKIP"]},
    "confidence": {"enum": ["high", "low"]},
}}

# the schema the real haiku code-review output actually targets
_REVIEW_SCH = {"properties": {
    "verdict": {"enum": ["FIX", "SKIP", "ESCALATE"]},
    "severity": {"enum": ["high", "medium", "low"]},
    "confidence": {"type": "integer"},
    "reason": {"type": "string"},
}}


def resolver_gates_bare_values() -> None:
    r = BareValueResolver()
    # enum: only a permitted member
    assert r.resolve("verdict", "FIX", _SCH) == (True, "FIX")
    assert r.resolve("verdict", "NOPE", _SCH) == (False, None)
    # a type:string field accepts a free-form bare value (the common real case)
    sch = {"properties": {"reason": {"type": "string"}}}
    assert r.resolve("reason", "anything at all", sch) == (True, "anything at all")
    # a typed-non-string field never swallows a bare word (no silent coercion)
    assert r.resolve("n", "high", {"properties": {"n": {"type": "integer"}}}) == (False, None)
    # no schema / unknown field -> never fabricated
    assert r.resolve("verdict", "FIX", None) == (False, None)
    assert r.resolve("missing", "FIX", _SCH) == (False, None)


# ── #3b UnquotedKeyValue plucker — composes Scanner + BareValueResolver ──────

def plucker_recovers_bare_enum_values() -> None:
    p = UnquotedKeyValue()
    assert p.parse('{verdict: FIX, confidence: high}', schema=_SCH) == {
        "verdict": "FIX", "confidence": "high"}
    # no surrounding braces (haiku prose-free line)
    assert p.parse('verdict: FIX', schema=_SCH) == {"verdict": "FIX"}
    # newline-separated fields
    assert p.parse('verdict: FIX\nconfidence: low', schema=_SCH) == {
        "verdict": "FIX", "confidence": "low"}


def plucker_coerces_safe_literals_without_schema() -> None:
    # numbers / bools / null are unambiguous literals — safe without an enum
    assert UnquotedKeyValue().parse('{count: 5, ok: true, gone: null}') == {
        "count": 5, "ok": True, "gone": None}
    # already-quoted values pass straight through
    assert UnquotedKeyValue().parse('{verdict: "FIX"}') == {"verdict": "FIX"}
    assert UnquotedKeyValue().parse("{note: 'hi'}") == {"note": "hi"}


def plucker_reads_real_haiku_line_output() -> None:
    # the EXACT shape real haiku emitted that my parser wrongly rejected (0/4)
    raw = ("verdict: FIX\n"
           "severity: high\n"
           "confidence: 100\n"
           "reason: SQL injection vulnerability — user input concatenated directly "
           "into query string without parameterization or escaping")
    assert UnquotedKeyValue().parse(raw, schema=_REVIEW_SCH) == {
        "verdict": "FIX", "severity": "high", "confidence": 100,
        "reason": ("SQL injection vulnerability — user input concatenated directly "
                   "into query string without parameterization or escaping"),
    }


def plucker_value_may_contain_colon() -> None:
    # the real 'style' reason carried a colon inside it — must not re-split on it
    sch = {"properties": {"reason": {"type": "string"}}}
    assert UnquotedKeyValue().parse(
        "reason: use `for item in items:` instead", schema=sch) == {
        "reason": "use `for item in items:` instead"}


def plucker_value_may_contain_comma_in_line_protocol() -> None:
    # newline-delimited fields: a comma is part of the value, not a field break
    sch = {"properties": {"verdict": {"enum": ["FIX"]}, "reason": {"type": "string"}}}
    assert UnquotedKeyValue().parse("verdict: FIX\nreason: a, b, and c", schema=sch) == {
        "verdict": "FIX", "reason": "a, b, and c"}


def plucker_tolerates_trailing_commas() -> None:
    sch = {"properties": {"verdict": {"enum": ["FIX"]}, "severity": {"enum": ["high"]}}}
    assert UnquotedKeyValue().parse("verdict: FIX,\nseverity: high,", schema=sch) == {
        "verdict": "FIX", "severity": "high"}


def plucker_never_fabricates_free_form() -> None:
    assert UnquotedKeyValue().parse('{verdict: MAYBE}', schema=_SCH) is None  # not in enum
    assert UnquotedKeyValue().parse('{verdict: FIX}') is None                 # bare word, no schema
    assert UnquotedKeyValue().parse('{notes: looks risky}', schema=_SCH) is None  # unknown key


def plucker_uses_injected_dependencies() -> None:
    # composable subclass + injection: swap the resolver policy, behaviour changes
    class AcceptAny(BareValueResolver):
        def resolve(self, field, word, schema):
            return (True, word)

    p = UnquotedKeyValue(resolver=AcceptAny())
    assert p.parse('{verdict: ANYTHING}') == {"verdict": "ANYTHING"}


def main() -> None:
    detects_decoy_keys_by_affix()
    detects_decoy_objects()
    affixes_are_configurable_one_line()
    balanced_spans_finds_top_level_objects()
    balanced_spans_respects_strings_and_nesting()
    match_returns_corresponding_close()
    split_top_level_splits_only_at_depth_zero()
    split_top_level_takes_multiple_separators()
    resolver_gates_bare_values()
    plucker_recovers_bare_enum_values()
    plucker_coerces_safe_literals_without_schema()
    plucker_reads_real_haiku_line_output()
    plucker_value_may_contain_colon()
    plucker_value_may_contain_comma_in_line_protocol()
    plucker_tolerates_trailing_commas()
    plucker_never_fabricates_free_form()
    plucker_uses_injected_dependencies()
    print("ok")


if __name__ == "__main__":
    main()
