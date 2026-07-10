"""golden_diff_rows — diff a campaign's collected outputs against a pinned golden (AB-4).

Used by: `yaah ab <experiment.json> --golden <expected.json>` and programmatic
callers.
Where: yaah.experiment — a pure read over the rows the runner collected, in the
same idiom as the report (build_matrix) and rescore: it groups rows by
(variant, fingerprint) population and runs nothing, spends nothing.
Why: the client's observability gap — there was no GOLDEN/expected artifact to
diff a run against. You pin a known-good output payload as a plain JSON file;
this reports, per population, how many collected runs MATCH the golden and, for
the ones that drifted, a compact, bounded, machine-readable diff. It is a
REPORT, not a CI gate (the A/B module's stance — the matrix presents, the human
decides): `n_differ == 0` is your green, but the read never fails on drift. A
caller that wants a gate keys off `n_differ` itself.

The diff direction is GOLDEN → ACTUAL (the run's output payload):
  - `removed` — a key the golden EXPECTS that the run did not produce
  - `added`   — a key the run produced BEYOND the golden
  - `changed` — a key present in both whose value drifted; each carries a
                bounded {golden, actual} summary pair
Objects are recursed (drift lands on a dotted path, e.g. `meta.score`); a path
that blows up is CAPPED with a `__truncated__` sentinel so a 500-key explosion
can't produce 500 paths.

Bounded value summaries (no multi-KB dumps):
  - scalars (null/bool/int/float, short string) — the raw value
  - a string over 120 chars — {"str": <first 120>, "len": N, "truncated": true}
  - a list  — {"array": <len>}   (lists are compared as WHOLE values, see below)
  - a dict  — {"object": <key count>}

Config-declared SCRUB list (`scrub` in the experiment config, or the `scrub`
override arg): volatile keys — timestamps, ids, correlation ids, run paths —
declared as DATA and removed from BOTH the golden and every output before
diffing, so the golden doesn't churn on every run. A scrub entry is a dotted
key PATH; v1 is meant for top-level keys and one nesting level (e.g. `"ts"`,
`"meta.corr"`) — deeper paths work but that shallow shape is the intended
simplicity. A scrub key that is present NOWHERE (golden or any output) is
WARNED, not silently ignored (a misdeclared volatile key would otherwise
churn the diff and get blamed on the pipeline).

KNOWN v1 LIMITATIONS (documented, not bugs):
  - Values are compared with `==`. A golden diff assumes a (near-)deterministic
    output — pin the golden from a real run and diff FAKE/deterministic campaigns,
    or expect a stochastic model to drift every leaf. There is no float epsilon
    tolerance in v1; `scrub` is the lever for the volatile keys you can name.
    A `NaN` leaf (json.load accepts it) compares unequal to itself, so it reads
    as perpetually `changed` — scrub it or fix the producer.
  - LISTS are compared as whole values (summarized as {"array": len}), NOT
    recursed by index — a single element drift reads as a whole-array change.
    Index-level array diffing is deferred (it fights the bounded-output goal).
  - One golden is diffed against EVERY row; a cell spanning multiple inputs is
    WARNED (a single golden can't be right for differing inputs — pin a
    single-input experiment).

Targets Python 3.9+.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Optional, Set, Tuple

from .experiment_store import ExperimentStore

_MAX_STR_PREVIEW = 120   # a longer string is summarized, never dumped
_MAX_PATHS = 50          # per category (added/removed/changed) per distinct diff
_MAX_DISTINCT = 20       # distinct drift shapes reported per cell


def _validate_scrub(scrub: Any) -> List[str]:
    """Shape-check a scrub list. Returns error strings (empty = ok) so both the
    experiment-config check (runner._check_experiment) and the arg override
    share one definition of a valid scrub path."""
    if scrub is None:
        return []
    if not (isinstance(scrub, list)
            and all(isinstance(p, str) and p and all(p.split("."))
                    for p in scrub)):
        return ["`scrub` must be a list of dotted key paths with non-empty "
                "segments (e.g. [\"ts\", \"meta.corr\"]) — volatile keys removed "
                "from both golden and output before diffing; \".ts\" or \"a..b\" "
                "is a typo"]
    return []


def load_golden(path: str) -> Dict[str, Any]:
    """Read a pinned golden artifact from `path` — plain JSON (NOT the engine's
    `_extends`-resolving reader: a golden is a captured output payload, not a
    config), failing LOUD on a missing file or a non-object with a message that
    says the fix."""
    import os
    if not os.path.exists(path):
        raise ValueError(
            "golden artifact not found: {!r} — pin the expected output payload "
            "as a JSON file (copy a known-good run's output), then "
            "`yaah ab <exp.json> --golden {}`".format(path, path))
    try:
        with open(path) as f:
            golden = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(
            "golden artifact {!r} is not valid JSON: {}".format(path, e)) from e
    except OSError as e:   # a directory, permission denied, … — loud, not a traceback
        raise ValueError(
            "golden artifact {!r} is not a readable file ({}) — point --golden "
            "at the pinned JSON file itself".format(path, e)) from e
    if not isinstance(golden, dict):
        raise ValueError(
            "golden artifact must be a JSON OBJECT (the expected output "
            "payload) — got {} in {!r}".format(type(golden).__name__, path))
    return golden


def _summarize(value: Any) -> Any:
    """A bounded, JSON-safe summary of one value — the raw scalar when it's
    small, a shape token otherwise, so a multi-KB payload never lands in the
    diff."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= _MAX_STR_PREVIEW:
            return value
        return {"str": value[:_MAX_STR_PREVIEW], "len": len(value), "truncated": True}
    if isinstance(value, list):
        return {"array": len(value)}
    return {"object": len(value)}   # dict — the only remaining JSON type


def _diff(golden: Dict[str, Any], actual: Dict[str, Any],
          prefix: str = "") -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Recursive object diff, golden → actual. Recurses into dicts (drift lands
    on a dotted path); lists and scalars are leaves compared with `==`."""
    added: Dict[str, Any] = {}
    removed: Dict[str, Any] = {}
    changed: Dict[str, Any] = {}
    gkeys, akeys = set(golden), set(actual)
    for k in sorted(akeys - gkeys):
        added[prefix + k] = _summarize(actual[k])
    for k in sorted(gkeys - akeys):
        removed[prefix + k] = _summarize(golden[k])
    for k in sorted(gkeys & akeys):
        gv, av = golden[k], actual[k]
        path = prefix + k
        if isinstance(gv, dict) and isinstance(av, dict):
            a2, r2, c2 = _diff(gv, av, path + ".")
            added.update(a2)
            removed.update(r2)
            changed.update(c2)
        elif gv != av:
            changed[path] = {"golden": _summarize(gv), "actual": _summarize(av)}
    return added, removed, changed


def _cap(paths: Dict[str, Any]) -> Dict[str, Any]:
    """Bound one diff category to _MAX_PATHS entries with a `__truncated__`
    sentinel (a marker key carrying the overflow count) — a wholesale object
    replacement can't flood the report. The sentinel appears ONLY when the
    category overflowed; in that case a real payload path literally named
    `__truncated__` is evicted into the overflow count rather than clobbered
    (adversarial-eval finding: overwriting it corrupted the entry AND the
    count), so the count is always the exact number of omitted real paths.
    A non-overflowing category passes through untouched, real `__truncated__`
    keys included."""
    if len(paths) <= _MAX_PATHS:
        return paths
    entries = {k: v for k, v in paths.items() if k != "__truncated__"}
    kept = dict(sorted(entries.items())[:_MAX_PATHS])
    kept["__truncated__"] = len(paths) - _MAX_PATHS
    return kept


def _scrub(obj: Dict[str, Any], paths: List[str], hits: Set[str]) -> Dict[str, Any]:
    """Return a COPY of `obj` with every scrub path deleted; record which paths
    actually removed something in `hits` (so a never-hit path can be warned)."""
    out = copy.deepcopy(obj)
    for p in paths:
        segs = p.split(".")
        cur: Any = out
        for s in segs[:-1]:
            if isinstance(cur, dict) and s in cur:
                cur = cur[s]
            else:
                cur = None
                break
        if isinstance(cur, dict) and segs[-1] in cur:
            del cur[segs[-1]]
            hits.add(p)
    return out


async def golden_diff_rows(cfg: Dict[str, Any], base: str, golden: Dict[str, Any],
                           *, store: Optional[ExperimentStore] = None,
                           scrub: Optional[List[str]] = None) -> Dict[str, Any]:
    """Diff every collected row's output payload against `golden`.
    Returns {experiment, scrub, cells: [...], warnings}. Each cell is a
    (variant, fingerprint) population: {variant, fingerprint, n, n_match,
    n_differ, n_no_output, diffs}, where `diffs` deduplicates identical drift
    shapes into {count, added, removed, changed}. `scrub` overrides the
    experiment config's `scrub` list (both are volatile-key paths removed from
    golden and output before diffing)."""
    from .runner import _check_experiment

    _check_experiment(cfg)
    if not isinstance(golden, dict):
        raise ValueError(
            "golden must be a JSON OBJECT (the expected output payload) — "
            "got {}".format(type(golden).__name__))
    paths = list(scrub) if scrub is not None else list(cfg.get("scrub") or [])
    errs = _validate_scrub(paths)
    if errs:
        raise ValueError("invalid golden diff: " + "; ".join(errs))

    exp_id = cfg["id"]
    hits: Set[str] = set()
    g_scrubbed = _scrub(golden, paths, hits)

    from .store_factory import opened_store
    async with opened_store(cfg, base, store) as st:
        rows = await st.rows(exp_id)

    groups: Dict[Any, List[Dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault((row["variant"], row["fingerprint"]), []).append(row)

    cells: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for (variant, fingerprint), grows in sorted(groups.items()):
        input_ids: Set[Any] = set()
        no_output = 0
        n_match = 0
        distinct: Dict[str, Dict[str, Any]] = {}
        for r in grows:
            input_ids.add(r.get("input_id"))
            output = r.get("output")
            if not isinstance(output, dict):
                no_output += 1
                continue
            added, removed, changed = _diff(g_scrubbed, _scrub(output, paths, hits))
            if not (added or removed or changed):
                n_match += 1
                continue
            diff = {"added": _cap(added), "removed": _cap(removed),
                    "changed": _cap(changed)}
            key = json.dumps(diff, sort_keys=True)
            slot = distinct.setdefault(key, {"diff": diff, "count": 0})
            slot["count"] += 1

        diffs = [dict(s["diff"], count=s["count"]) for s in distinct.values()]
        diffs.sort(key=lambda dd: (-dd["count"], json.dumps(dd, sort_keys=True)))
        if len(diffs) > _MAX_DISTINCT:
            warnings.append(
                "cell ({!r}, {}…): {} distinct drift shapes — showing the top "
                "{}".format(variant, fingerprint[:12], len(diffs), _MAX_DISTINCT))
            diffs = diffs[:_MAX_DISTINCT]
        n_differ = (len(grows) - no_output) - n_match
        cells.append({"variant": variant, "fingerprint": fingerprint,
                      "n": len(grows), "n_match": n_match, "n_differ": n_differ,
                      "n_no_output": no_output, "diffs": diffs})
        if no_output:
            warnings.append(
                "cell ({!r}, {}…): {} row(s) have no output payload "
                "(suspended/error runs) and were not diffed".format(
                    variant, fingerprint[:12], no_output))
        if len(input_ids) > 1:
            warnings.append(
                "cell ({!r}, {}…) spans {} inputs {} — one golden diffed against "
                "differing inputs; pin a single-input experiment or expect "
                "per-input drift".format(
                    variant, fingerprint[:12], len(input_ids),
                    sorted(repr(i) for i in input_ids if i is not None)))

    for p in sorted(set(paths) - hits):
        warnings.append(
            "scrub key {!r} was never present in the golden or any diffed output "
            "— a typo, the pipeline renamed it, or it sits under a LIST "
            "(scrub descends objects only)".format(p))

    return {"experiment": exp_id, "scrub": sorted(paths),
            "cells": cells, "warnings": warnings}
