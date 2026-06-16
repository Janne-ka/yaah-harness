"""recall — score a candidate variant's findings against a baseline (A/B + regression).

Used by: A/B variant experiments and regression checks (run vs a stored baseline).
A/B is NOT "swap the model" — the arms are different VARIANTS: they differ by PROMPT
(and optionally model), and one arm may be a multi-prompt sub-pipeline where the other
is a single prompt. The arms run in PARALLEL (the harness fan-out runs them
concurrently); this then scores how many of the baseline arm's real findings a
candidate arm still caught. Pairs with `yaah.trace.aggregate` for the COST half —
together they answer "is the leaner/cheaper variant good enough, and how much
cheaper?".
Run directly: `python -m yaah.recall baseline.json candidate.json [--key id]
[--where verdict=REAL_BUG]`.
Where: the engine stdlib — PURE set comparison (no I/O except the thin CLI), so it
serves any "dataset -> graph -> diff" eval pattern, not just review findings.
Why: A/B = "run variant arms in parallel + a scorer". The run-in-parallel is already
the harness fan-out; this is the missing scorer. Recall = did the candidate find what
the baseline did; precision = how much of what it found was in the baseline.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


def by_field(name: str) -> Callable[[Dict[str, Any]], Any]:
    """A key function selecting one field — the usual way to identify a finding
    (e.g. its `id`, or a `file:line` location)."""
    return lambda item: item.get(name)


def field_equals(name: str, value: Any) -> Callable[[Dict[str, Any]], bool]:
    """A predicate keeping only items whose field equals a value — e.g. score recall
    over `verdict == "REAL_BUG"` so false positives don't count."""
    return lambda item: item.get(name) == value


_WHERE_RE = re.compile(r"^(.+?):(\d+)(?:[-,:].*)?$")


def parse_where(s: Any) -> Tuple[Optional[str], Optional[int]]:
    """Parse a finding's `where` location into (file_basename, line_or_None).
    Tolerant of the shapes review prompts produce: `src/bank.py:42` →
    (`bank.py`, 42); `bank.py:5-7` → (`bank.py`, 5); `bank.py:5: KeyError` →
    (`bank.py`, 5); `bank.py` → (`bank.py`, None); empty/non-string → (None,
    None). Path is reduced to basename so an arm that names `src/bank.py` and
    another naming `app/src/bank.py` still match — bug LOCATIONS are what we're
    keying on, not paths."""
    if not isinstance(s, str) or not s.strip():
        return (None, None)
    s = s.strip()
    m = _WHERE_RE.match(s)
    if m:
        return (os.path.basename(m.group(1)), int(m.group(2)))
    path = s.rstrip(":")
    return (os.path.basename(path) if path else None, None)


def location_matcher(*, tolerance: int = 3, field: str = "where",
                     ) -> Callable[[Dict[str, Any], Dict[str, Any]], bool]:
    """A `compare(matcher=)` for cross-arm finding identity by LOCATION. Two
    findings match when their `where` parses to the same file basename AND
    their lines are within `tolerance` (inclusive). Falls back to file-only
    match when either side lacks a line. Findings with no parseable `where`
    on either side never match — they stay in missed/extra honestly. Default
    tolerance ±3 is what the lens prompts already practice (different lenses
    point at the read site vs the use site of the same defect)."""
    def _matches(b_item: Dict[str, Any], c_item: Dict[str, Any]) -> bool:
        bf, bl = parse_where(b_item.get(field))
        cf, cl = parse_where(c_item.get(field))
        if not bf or not cf or bf != cf:
            return False
        if bl is None or cl is None:
            return True
        return abs(bl - cl) <= tolerance
    return _matches


def compare(baseline: Sequence[Dict[str, Any]], candidate: Sequence[Dict[str, Any]],
            *, key: Optional[Callable[[Dict[str, Any]], Any]] = None,
            matcher: Optional[Callable[[Dict[str, Any], Dict[str, Any]], bool]] = None,
            where: Optional[Callable[[Dict[str, Any]], bool]] = None) -> Dict[str, Any]:
    """Score `candidate` against `baseline` (optionally filtered by `where`).
    Two identity modes — pick one:
    - `key`: equality on a derived value (the classic case; `by_field("id")` is
      the default if neither is given). Fast, exact, used by the regression CLI.
    - `matcher`: a predicate `(baseline_item, candidate_item) -> bool` for fuzzy
      identity (e.g. `location_matcher()` for file:line ±tolerance). Greedy 1:1
      pairing in baseline order — each baseline item consumes the first matching
      unused candidate so two distinct bugs on the same line don't double-count.
    Returns recall / precision / counts + the actual missed & extra items.
    Empty baseline → recall 1.0 (nothing to miss); empty candidate → precision
    1.0. Pure; deterministic."""
    sel = where or (lambda _x: True)
    b = [x for x in baseline if sel(x)]
    c = [x for x in candidate if sel(x)]
    # Normalize both modes to one greedy 1:1 pairing — items in baseline order
    # consume the FIRST matching unused candidate. This preserves the natural
    # count: a baseline with N items has n_baseline == N (no silent collapse on
    # shared keys, and the metric denominator stays honest). For the key path,
    # items where `key` returns None never match — treating "no identity" as a
    # match would link two unrelated findings whose author just forgot the id.
    if matcher is None:
        k = key or by_field("id")

        def _matches(bi: Dict[str, Any], ci: Dict[str, Any]) -> bool:
            kb = k(bi)
            return kb is not None and kb == k(ci)
    else:
        _matches = matcher
    used: set = set()
    hits_items: List[Dict[str, Any]] = []
    missed: List[Dict[str, Any]] = []
    for bi in b:
        idx = next((i for i, ci in enumerate(c) if i not in used and _matches(bi, ci)), None)
        if idx is None:
            missed.append(bi)
        else:
            used.add(idx)
            hits_items.append(bi)
    extra = [c[i] for i in range(len(c)) if i not in used]
    n_hits = len(hits_items)
    return {
        "recall": n_hits / len(b) if b else 1.0,
        "precision": n_hits / len(c) if c else 1.0,
        "n_baseline": len(b), "n_candidate": len(c), "n_hit": n_hits,
        "missed": missed, "extra": extra,
    }


def _findings(doc: Any) -> List[Dict[str, Any]]:
    """Accept either a bare list of findings or an object with a `findings` list
    (the eval stage's output shape)."""
    if isinstance(doc, dict):
        return list(doc.get("findings", []))
    return list(doc or [])


def main() -> None:  # pragma: no cover - thin CLI over the tested core
    import sys
    args = sys.argv[1:]
    if len(args) < 2:
        print("usage: python -m yaah.recall <baseline.json> <candidate.json> "
              "[--key FIELD] [--where FIELD=VALUE]")
        raise SystemExit(2)
    with open(args[0]) as f:
        baseline = _findings(json.load(f))
    with open(args[1]) as f:
        candidate = _findings(json.load(f))
    key_field, where = "id", None
    rest = args[2:]
    while rest:
        if rest[0] == "--key" and len(rest) > 1:
            key_field, rest = rest[1], rest[2:]
        elif rest[0] == "--where" and len(rest) > 1:
            name, _, value = rest[1].partition("=")
            where, rest = field_equals(name, value), rest[2:]
        else:
            rest = rest[1:]
    result = compare(baseline, candidate, key=by_field(key_field), where=where)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
