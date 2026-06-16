"""recall.compare: A/B / regression scoring of findings against a baseline.

Run: cd yaah && PYTHONPATH=src python3 tests/test_recall.py
"""
from __future__ import annotations

from yaah.recall import by_field, compare, field_equals, location_matcher, parse_where


def scenario_recall_and_precision() -> None:
    baseline = [{"id": "A", "verdict": "REAL_BUG"}, {"id": "B", "verdict": "REAL_BUG"},
                {"id": "C", "verdict": "REAL_BUG"}]
    candidate = [{"id": "A"}, {"id": "C"}, {"id": "Z"}]  # caught A,C; missed B; extra Z
    r = compare(baseline, candidate, key=by_field("id"))
    assert r["n_baseline"] == 3 and r["n_candidate"] == 3 and r["n_hit"] == 2
    assert abs(r["recall"] - 2 / 3) < 1e-9          # found 2 of 3 baseline
    assert abs(r["precision"] - 2 / 3) < 1e-9       # 2 of 3 candidates were real
    assert [m["id"] for m in r["missed"]] == ["B"]
    assert [e["id"] for e in r["extra"]] == ["Z"]


def scenario_where_filters_to_real_bugs() -> None:
    # only REAL_BUGs count toward recall — a candidate's false positives are ignored
    baseline = [{"id": "A", "verdict": "REAL_BUG"}, {"id": "B", "verdict": "FALSE_POSITIVE"}]
    candidate = [{"id": "A", "verdict": "REAL_BUG"}, {"id": "Q", "verdict": "FALSE_POSITIVE"}]
    r = compare(baseline, candidate, key=by_field("id"),
                where=field_equals("verdict", "REAL_BUG"))
    assert r["n_baseline"] == 1 and r["n_candidate"] == 1 and r["recall"] == 1.0
    assert r["precision"] == 1.0 and r["missed"] == [] and r["extra"] == []


def scenario_empty_edges() -> None:
    assert compare([], [{"id": "X"}], key=by_field("id"))["recall"] == 1.0   # nothing to miss
    assert compare([{"id": "X"}], [], key=by_field("id"))["recall"] == 0.0   # missed everything
    assert compare([{"id": "X"}], [], key=by_field("id"))["precision"] == 1.0


def scenario_parse_where_handles_common_shapes() -> None:
    # the shapes the lens prompts actually emit (file:line / file:range / no line)
    assert parse_where("src/bank.py:42") == ("bank.py", 42)
    assert parse_where("bank.py:5-7") == ("bank.py", 5)            # range -> first line
    assert parse_where("bank.py:5: KeyError") == ("bank.py", 5)    # trailing context
    assert parse_where("bank.py") == ("bank.py", None)             # no line
    assert parse_where("bank.py:") == ("bank.py", None)            # trailing colon
    assert parse_where("") == (None, None)                          # empty
    assert parse_where(None) == (None, None)                        # missing


def scenario_location_matcher_within_tolerance() -> None:
    # the A/B identity bug: A says bank.py:5, B says bank.py:7 — same defect
    # (one points at the read site, the other at the use site).
    m = location_matcher(tolerance=3)
    assert m({"where": "bank.py:5"}, {"where": "bank.py:7"}) is True
    assert m({"where": "src/bank.py:5"}, {"where": "app/bank.py:7"}) is True  # basename
    assert m({"where": "bank.py:5"}, {"where": "bank.py:9"}) is False          # outside ±3
    assert m({"where": "bank.py:5"}, {"where": "other.py:5"}) is False         # diff file
    # file-only fallback: one side has no line, the other does
    assert m({"where": "bank.py"}, {"where": "bank.py:42"}) is True
    # no parseable where: never a match (don't fake identity)
    assert m({"where": ""}, {"where": "bank.py:5"}) is False
    assert m({}, {"where": "bank.py:5"}) is False


def scenario_compare_by_location_recovers_recall_when_ids_differ() -> None:
    # the actual session bug: A and B name the same defects with different ids,
    # so id-keyed compare reports recall=0. Location-keyed compare recovers it.
    baseline = [
        {"id": "D-1", "verdict": "REAL_BUG", "where": "bank.py:5"},
        {"id": "D-2", "verdict": "REAL_BUG", "where": "bank.py:6"},
    ]
    candidate = [
        {"id": "negative-amount", "verdict": "REAL_BUG", "where": "bank.py:6"},
        {"id": "MISSING_AUTH",    "verdict": "REAL_BUG", "where": "bank.py:7"},
        {"id": "noise",           "verdict": "REAL_BUG", "where": "bank.py:30"},
    ]
    # id-keyed: the bug we set out to fix
    r_id = compare(baseline, candidate, key=by_field("id"),
                   where=field_equals("verdict", "REAL_BUG"))
    assert r_id["recall"] == 0.0 and r_id["n_hit"] == 0
    # location-keyed: both A findings matched (one within ±3 of D-1, one exact of D-2)
    r_loc = compare(baseline, candidate, matcher=location_matcher(tolerance=3),
                    where=field_equals("verdict", "REAL_BUG"))
    assert r_loc["recall"] == 1.0 and r_loc["n_hit"] == 2
    # AND we still see the EXTRA finding (the noise far from any baseline)
    assert [e["id"] for e in r_loc["extra"]] == ["noise"]
    # precision = 2/3 — half-credit for the extra
    assert abs(r_loc["precision"] - 2 / 3) < 1e-9


def scenario_matcher_pairs_one_to_one() -> None:
    # two distinct baseline findings on adjacent lines must not both consume the
    # same candidate — greedy 1:1 pairing, in baseline order.
    baseline = [{"where": "bank.py:5"}, {"where": "bank.py:6"}]
    candidate = [{"where": "bank.py:5"}]                   # only one match available
    r = compare(baseline, candidate, matcher=location_matcher(tolerance=3))
    assert r["n_hit"] == 1 and len(r["missed"]) == 1


def scenario_key_path_preserves_natural_count_on_duplicates() -> None:
    # assessment #5: the OLD key path was {key(x): x for x in baseline} which
    # silently collapsed two findings sharing a key (n_baseline=1 vs the actual 2).
    # The headline A/B metric was then computed over a wrong, smaller denominator.
    baseline = [{"id": "A"}, {"id": "A"}, {"id": "B"}]   # two distinct A findings (real)
    candidate = [{"id": "A"}]                            # candidate only caught one
    r = compare(baseline, candidate, key=by_field("id"))
    assert r["n_baseline"] == 3, r                       # no silent dedup
    assert r["n_hit"] == 1                               # greedy 1:1 — only one A matched
    assert r["recall"] == 1 / 3                          # 1 of 3 found
    assert len(r["missed"]) == 2                         # the second A + the B


def scenario_key_path_missing_field_never_matches() -> None:
    # the OLD key path bucketed every item with no `id` under key=None, so
    # missing-id baselines all "matched" missing-id candidates spuriously.
    # New rule: key(x) is None -> never matches (honest miss, like the matcher
    # path treats unparseable `where`).
    baseline = [{"id": None, "note": "x"}, {"id": "B"}]
    candidate = [{"id": None, "note": "y"}, {"id": "B"}]
    r = compare(baseline, candidate, key=by_field("id"))
    assert r["n_baseline"] == 2 and r["n_candidate"] == 2
    assert r["n_hit"] == 1                               # only the B/B pair counts
    assert [m.get("id") for m in r["missed"]] == [None]
    assert [e.get("id") for e in r["extra"]] == [None]


def main() -> None:
    scenario_recall_and_precision()
    scenario_where_filters_to_real_bugs()
    scenario_empty_edges()
    scenario_parse_where_handles_common_shapes()
    scenario_location_matcher_within_tolerance()
    scenario_compare_by_location_recovers_recall_when_ids_differ()
    scenario_matcher_pairs_one_to_one()
    scenario_key_path_preserves_natural_count_on_duplicates()
    scenario_key_path_missing_field_never_matches()
    print("ok")


if __name__ == "__main__":
    main()
