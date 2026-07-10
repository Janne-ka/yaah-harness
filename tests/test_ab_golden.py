"""golden_diff_rows — diff a campaign's collected outputs against a pinned golden (AB-4).

The use-case (the client's observability gap: "no golden/expected artifact to
diff a run against"): you pin a known-good output payload as a JSON file; the
golden diff reports, per (variant, fingerprint) population, how many collected
runs MATCH it and — for the ones that drifted — a compact, bounded, machine-
readable diff (added / removed / changed keys, values summarized so a multi-KB
payload can't flood the report).

Contracts under test (falsification-first):
- identical output vs golden diffs EMPTY (n_match == n)
- a changed NESTED value is caught, on its dotted path
- an added key and a removed (missing-from-run) key are both caught
- config-declared scrub keys are removed from BOTH sides before diffing, so a
  volatile key that DIFFERS never shows up in the diff (no churn)
- a MISSING golden file fails loud with an actionable message; so does a
  directory or unreadable path (eval B1) and a non-object golden
- a real key named "__truncated__" is never clobbered by the cap sentinel
  (eval B2); the overflow count stays exact
- the same rows diffed twice serialize IDENTICALLY (determinism pin)
- huge values are SIZE-BOUNDED in the diff (long string truncated; big
  container summarized as a shape token — never dumped)
- suspended/error rows (output is None) are counted n_no_output, never guessed
- identical diffs across repetitions DEDUPE (one entry, a count)
- CLI: --golden renders + --json emits the object; mutually exclusive with
  --report / --rescore

Run: cd yaah && PYTHONPATH=src python3 tests/test_ab_golden.py

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys as _sys
import tempfile

from yaah.adapters.experiment_stores import JsonlExperimentStore
from yaah.experiment.golden import golden_diff_rows, load_golden


def _row(**over):
    base = {"experiment_id": "g", "variant": "A", "fingerprint": "f1",
            "input_id": "i", "rep": 0, "t_start": 0, "t_end": 1,
            "outcome": "done"}
    base.update(over)
    return base


def _store_with(d, rows):
    st = JsonlExperimentStore(d)
    for r in rows:
        asyncio.run(st.append_row("g", r))
    return st


def _exp(scrub=None):
    exp = {"id": "g", "variants": {"A": "unused.json"}, "inputs": [{}],
           "price_map": {}, "store": {"dir": "."}}
    if scrub is not None:
        exp["scrub"] = scrub
    return exp


def main() -> None:
    # 1. identical output vs golden -> empty diff, all match
    with tempfile.TemporaryDirectory() as d:
        st = _store_with(d, [
            _row(output={"verdict": "ship", "meta": {"score": 3}}),
            _row(rep=1, output={"verdict": "ship", "meta": {"score": 3}}),
        ])
        golden = {"verdict": "ship", "meta": {"score": 3}}
        r = asyncio.run(golden_diff_rows(_exp(), d, golden, store=st))
        c = r["cells"][0]
        assert c["n"] == 2 and c["n_match"] == 2 and c["n_differ"] == 0, c
        assert c["diffs"] == [], c

    # 2. a changed NESTED value is caught on its dotted path
    with tempfile.TemporaryDirectory() as d:
        st = _store_with(d, [_row(output={"verdict": "ship", "meta": {"score": 9}})])
        golden = {"verdict": "ship", "meta": {"score": 3}}
        r = asyncio.run(golden_diff_rows(_exp(), d, golden, store=st))
        c = r["cells"][0]
        assert c["n_differ"] == 1 and c["n_match"] == 0, c
        diff = c["diffs"][0]
        assert diff["count"] == 1, diff
        assert "meta.score" in diff["changed"], diff
        assert diff["changed"]["meta.score"] == {"golden": 3, "actual": 9}, diff
        assert diff["added"] == {} and diff["removed"] == {}, diff

    # 3. added key (run produced extra) + removed key (run dropped an expected one)
    with tempfile.TemporaryDirectory() as d:
        st = _store_with(d, [_row(output={"verdict": "ship", "extra": 1})])
        golden = {"verdict": "ship", "wanted": True}
        r = asyncio.run(golden_diff_rows(_exp(), d, golden, store=st))
        diff = r["cells"][0]["diffs"][0]
        assert "extra" in diff["added"], diff       # run produced beyond golden
        assert "wanted" in diff["removed"], diff     # golden expected, run omitted
        assert diff["changed"] == {}, diff

    # 4. scrub removes volatile keys from BOTH sides -> a differing volatile key
    #    does NOT churn the diff (top-level + one nesting level)
    with tempfile.TemporaryDirectory() as d:
        st = _store_with(d, [
            _row(output={"verdict": "ship", "ts": 111, "meta": {"corr": "run-A", "k": 1}}),
        ])
        golden = {"verdict": "ship", "ts": 999, "meta": {"corr": "GOLD", "k": 1}}
        r = asyncio.run(golden_diff_rows(
            _exp(scrub=["ts", "meta.corr"]), d, golden, store=st))
        c = r["cells"][0]
        assert c["n_match"] == 1 and c["diffs"] == [], c
        assert r["scrub"] == ["meta.corr", "ts"], r["scrub"]
        # and WITHOUT scrub the same volatile keys DO show as changed (proof the
        # scrub is what suppressed them, not that they happened to match)
        r2 = asyncio.run(golden_diff_rows(_exp(), d, golden, store=st))
        diff = r2["cells"][0]["diffs"][0]
        assert "ts" in diff["changed"] and "meta.corr" in diff["changed"], diff

    # 5. MISSING golden file fails loud
    with tempfile.TemporaryDirectory() as d:
        try:
            load_golden(os.path.join(d, "nope.json"))
            raise AssertionError("missing golden must fail loud")
        except ValueError as e:
            assert "not found" in str(e).lower(), e

    # 5a. a DIRECTORY (exists, unreadable as a file) fails loud too — eval catch
    #     B1: os.path.exists passed it, so open() raised a raw IsADirectoryError
    with tempfile.TemporaryDirectory() as d:
        try:
            load_golden(d)
            raise AssertionError("directory golden must fail loud")
        except ValueError as e:
            assert "readable" in str(e).lower(), e

    # 5b. non-object golden rejected (both helper and function)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "bad.json")
        with open(p, "w") as f:
            json.dump([1, 2, 3], f)
        try:
            load_golden(p)
            raise AssertionError("non-object golden must be rejected")
        except ValueError as e:
            assert "object" in str(e).lower(), e
        st = _store_with(d, [_row(output={"a": 1})])
        try:
            asyncio.run(golden_diff_rows(_exp(), d, [1, 2], store=st))
            raise AssertionError("non-object golden must be rejected by function")
        except ValueError as e:
            assert "object" in str(e).lower(), e

    # 6. huge LEAF values are SIZE-BOUNDED — summarized, never dumped
    with tempfile.TemporaryDirectory() as d:
        big_str = "x" * 5000
        big_list = list(range(1000))
        big_obj = {"k{}".format(i): i for i in range(500)}
        st = _store_with(d, [_row(output={
            "s": big_str, "l": big_list, "o": big_obj})])
        # golden pins scalars where the run drifted to giant values
        golden = {"s": "short", "l": 1, "o": 1}
        r = asyncio.run(golden_diff_rows(_exp(), d, golden, store=st))
        ch = r["cells"][0]["diffs"][0]["changed"]
        # the actual side is summarized, never the raw multi-KB value
        assert ch["s"]["actual"]["truncated"] is True, ch["s"]
        assert ch["s"]["actual"]["len"] == 5000, ch["s"]
        assert len(ch["s"]["actual"]["str"]) <= 200, ch["s"]
        assert ch["l"]["actual"] == {"array": 1000}, ch["l"]
        assert ch["o"]["actual"] == {"object": 500}, ch["o"]
        # the whole serialized diff stays small regardless of payload size
        assert len(json.dumps(r)) < 4000, len(json.dumps(r))

    # 6b. a huge NESTED-object drift recurses but the path list is CAPPED with a
    #     sentinel — a 500-key blow-up can't produce 500 diff paths
    with tempfile.TemporaryDirectory() as d:
        st = _store_with(d, [_row(output={"k{}".format(i): i for i in range(500)})])
        r = asyncio.run(golden_diff_rows(_exp(), d, {}, store=st))
        added = r["cells"][0]["diffs"][0]["added"]
        assert "__truncated__" in added, list(added)[:5]
        assert len(added) <= 51, len(added)   # cap + the sentinel
        assert added["__truncated__"] > 0, added["__truncated__"]

    # 6c. a REAL payload key literally named "__truncated__" in an overflowing
    #     category is EVICTED into the overflow count, never clobbered by the
    #     sentinel (eval catch B2: it was overwritten AND the count was wrong)
    with tempfile.TemporaryDirectory() as d:
        out = {"k{:03d}".format(i): i for i in range(60)}
        out["__truncated__"] = "real-value"
        st = _store_with(d, [_row(output=out)])
        r = asyncio.run(golden_diff_rows(_exp(), d, {}, store=st))
        added = r["cells"][0]["diffs"][0]["added"]
        assert added["__truncated__"] == 61 - 50, added["__truncated__"]  # exact count
        assert "real-value" not in json.dumps(added), added  # never half-reported
        # and in a NON-overflowing category the real key passes through intact
        st2 = _store_with(d, [_row(rep=1, output={"__truncated__": "real-value"})])
        r2 = asyncio.run(golden_diff_rows(_exp(), d, {}, store=st2))
        # both rows are in the same store dir; find the small diff
        small = [dd for c in r2["cells"] for dd in c["diffs"]
                 if dd["added"].get("__truncated__") == "real-value"]
        assert small, r2["cells"]

    # 6d. DETERMINISM pinned: the same rows diffed twice serialize identically
    #     (dict/set ordering must never leak — eval asked for this regression pin)
    with tempfile.TemporaryDirectory() as d:
        st = _store_with(d, [
            _row(output={"b": 2, "a": 1, "nested": {"z": 9, "y": 8}}),
            _row(rep=1, output={"c": 3, "a": 1}),
            _row(rep=2, outcome="suspended", output=None),
        ])
        exp = _exp(scrub=["zz", "a"])
        r1 = asyncio.run(golden_diff_rows(exp, d, {"a": 0, "q": 7}, store=st))
        r2 = asyncio.run(golden_diff_rows(exp, d, {"a": 0, "q": 7}, store=st))
        assert json.dumps(r1, sort_keys=False) == json.dumps(r2, sort_keys=False)

    # 7. suspended / error rows (output None) counted n_no_output, not guessed
    with tempfile.TemporaryDirectory() as d:
        st = _store_with(d, [
            _row(output={"verdict": "ship"}),
            _row(rep=1, outcome="suspended", output=None),
            _row(rep=2, outcome="error", output=None),
        ])
        r = asyncio.run(golden_diff_rows(_exp(), d, {"verdict": "ship"}, store=st))
        c = r["cells"][0]
        assert c["n"] == 3 and c["n_no_output"] == 2 and c["n_match"] == 1, c
        assert any("no output" in w.lower() for w in r["warnings"]), r["warnings"]

    # 8. identical diffs across reps DEDUPE to one entry with a count
    with tempfile.TemporaryDirectory() as d:
        st = _store_with(d, [
            _row(output={"verdict": "hold"}),
            _row(rep=1, output={"verdict": "hold"}),
            _row(rep=2, output={"verdict": "drop"}),
        ])
        r = asyncio.run(golden_diff_rows(_exp(), d, {"verdict": "ship"}, store=st))
        c = r["cells"][0]
        assert c["n_differ"] == 3, c
        # two distinct drift shapes: verdict->hold (x2) and verdict->drop (x1)
        counts = sorted(dd["count"] for dd in c["diffs"])
        assert counts == [1, 2], counts

    # 9. scrub validated as config (bad shape -> loud, on any verb)
    with tempfile.TemporaryDirectory() as d:
        st = _store_with(d, [_row(output={"a": 1})])
        try:
            asyncio.run(golden_diff_rows(_exp(scrub=["a..b"]), d, {"a": 1}, store=st))
            raise AssertionError("empty scrub path segment must be rejected")
        except ValueError as e:
            assert "scrub" in str(e).lower(), e

    # 9b. a scrub key present NOWHERE (golden or output) warns — the typo trap
    #     (haiku catch: silent no-op scrub hides a misdeclared volatile key)
    with tempfile.TemporaryDirectory() as d:
        st = _store_with(d, [_row(output={"verdict": "ship"})])
        r = asyncio.run(golden_diff_rows(
            _exp(scrub=["nonexistent"]), d, {"verdict": "ship"}, store=st))
        assert any("nonexistent" in w and "never" in w.lower()
                   for w in r["warnings"]), r["warnings"]

    # 9c. one golden diffed across MULTIPLE inputs warns (a single golden can't
    #     be right for differing inputs) — honest about the ambiguity
    with tempfile.TemporaryDirectory() as d:
        st = _store_with(d, [
            _row(input_id="in-a", output={"v": 1}),
            _row(input_id="in-b", rep=1, output={"v": 2}),
        ])
        r = asyncio.run(golden_diff_rows(_exp(), d, {"v": 1}, store=st))
        assert any("spans" in w and "input" in w.lower()
                   for w in r["warnings"]), r["warnings"]

    # 10. CLI: --golden renders + --json emits the object; mutual exclusion
    from yaah.cli import _dispatch_ab, _parse_ab
    with tempfile.TemporaryDirectory() as d:
        # a real jsonl store the CLI will open from the exp config
        st = JsonlExperimentStore(os.path.join(d, ".ab"))
        asyncio.run(st.append_row("g", _row(output={"verdict": "ship"})))
        asyncio.run(st.append_row("g", _row(rep=1, output={"verdict": "hold"})))
        exp = {"id": "g", "variants": {"A": "unused.json"}, "inputs": [{}],
               "price_map": {}, "store": {"dir": ".ab"}, "scrub": ["ts"]}
        exp_path = os.path.join(d, "exp.json")
        with open(exp_path, "w") as f:
            json.dump(exp, f)
        gp = os.path.join(d, "golden.json")
        with open(gp, "w") as f:
            json.dump({"verdict": "ship"}, f)

        buf, old = io.StringIO(), _sys.stdout
        _sys.stdout = buf
        try:
            _dispatch_ab({"experiment": exp_path, "golden": gp})
        finally:
            _sys.stdout = old
        out = buf.getvalue()
        assert "match" in out.lower(), out

        spec = _parse_ab([exp_path, "--golden", gp, "--json"])
        assert spec["golden"] == gp and spec["json"]

        buf, old = io.StringIO(), _sys.stdout
        _sys.stdout = buf
        try:
            _dispatch_ab({"experiment": exp_path, "golden": gp, "json": True})
        finally:
            _sys.stdout = old
        obj = json.loads(buf.getvalue())
        assert obj["experiment"] == "g" and obj["cells"], obj

        for bad in ([exp_path, "--report", "--golden", gp],
                    [exp_path, "--rescore", gp, "--golden", gp],
                    [exp_path, "--golden"]):
            try:
                _parse_ab(bad)
                raise AssertionError("bad golden combo must be rejected: {}".format(bad))
            except SystemExit:
                pass

    print("PASS golden_diff_rows: key+value diff, config scrub, bounded summaries, "
          "no-output rows, dedupe, CLI wiring")


if __name__ == "__main__":
    main()
