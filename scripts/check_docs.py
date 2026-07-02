"""check_docs — validate JSON config snippets in the docs against the real engine.

Used by: contributors (and CI, once wired) to keep the docs honest. A fenced
```json / ```jsonc block that LOOKS like a yaah config is run through the real
`yaah.validate` entry points — a doc teaching a renamed key, a stale enum value,
or an unresolvable graph target fails the check instead of quietly mis-teaching
readers (the bug class: seven doc files once taught classes that no longer
existed).

Where: a sibling of `scripts/build_catalog.py` / `scripts/build_schemas.py` —
same pattern, opposite direction: those DERIVE docs/schemas from the engine
tables; this one CHECKS hand-written docs against the engine.

Why this convention (default = checked, opt OUT explicitly):
  - Every ```json / ```jsonc block is a candidate. A block is CHECKED when it
    parses as JSON and its shape says "yaah config":
      * has both "nodes" and "graph"      -> pipeline -> validate_pipeline
      * >= 2 of its top-level keys are in `_ROOT_KEYS` and they are the
        majority of its keys              -> root     -> validate_root
    (The majority rule keeps a genuine root with ONE typo'd key classified as
    root — so the typo FAILS instead of demoting the block to "fragment".)
  - Everything else is a FRAGMENT by shape (a bare `"stage": {...}` line, a
    payload example, the shape-grammar pseudo-config) and is skipped. No
    per-block opt-in marker — if checking were opt-in, drift would return.
  - Explicit opt-out for prose-adjacent pseudo-configs that WOULD classify:
    put `<!-- doc-snippet: skip -->` (or `example-only`) on the line
    immediately before the fence.
  - ```jsonc blocks get `//` line comments stripped before parsing (full-line
    comments, and trailing comments preceded by whitespace — so `"tls://…"`
    URLs survive). Known limit: a ` // ` INSIDE a string value would be
    mangled; that's a deliberate non-parser (none exist in the docs today).
  - `_extends` is NOT expanded — the raw object is validated as-is. `_`-keys
    are comment-convention and ignored by the validators, so a thin overlay
    snippet still checks the keys it does declare.

Run:  python3 scripts/check_docs.py            # validate; exit 1 on any fail
      python3 scripts/check_docs.py --list     # show every block + its fate

Targets Python 3.9+.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from typing import Any, Iterator, List, NamedTuple, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from yaah.validate import _ROOT_KEYS, validate_pipeline, validate_root  # noqa: E402

# indent allowed: fences inside list items ("   ```jsonc") count too
_FENCE_OPEN = re.compile(r"^\s*```(jsonc?)\s*$")
_SKIP_MARKER = re.compile(r"^\s*<!--\s*doc-snippet:\s*(skip|example-only)\s*-->\s*$")
# full-line // comment; and trailing // comment with whitespace BEFORE the
# slashes (so "tls://host" / "fn:mod:func" string content is never touched)
_LINE_COMMENT = re.compile(r"^\s*//.*$", re.M)
_TRAIL_COMMENT = re.compile(r"\s+//\s.*$", re.M)


class Snippet(NamedTuple):
    path: str          # relative to repo root
    line: int          # 1-based line of the opening fence
    lang: str          # "json" | "jsonc"
    body: str
    marked_skip: bool


class Result(NamedTuple):
    snippet: Snippet
    status: str        # "pass" | "skip" | "fail"
    detail: str


def strip_jsonc(text: str) -> str:
    return _TRAIL_COMMENT.sub("", _LINE_COMMENT.sub("", text))


def iter_snippets(path: str, rel: str) -> Iterator[Snippet]:
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    i = 0
    while i < len(lines):
        m = _FENCE_OPEN.match(lines[i])
        if not m:
            i += 1
            continue
        start = i + 1
        j = start
        while j < len(lines) and lines[j].strip() != "```":
            j += 1
        marked = i > 0 and bool(_SKIP_MARKER.match(lines[i - 1]))
        yield Snippet(rel, i + 1, m.group(1), "\n".join(lines[start:j]), marked)
        i = j + 1


def classify(obj: Any) -> Tuple[Optional[str], str]:
    """('pipeline'|'root', why) for a checkable config; (None, why) for a
    fragment. Shape-only — never inspects values."""
    if not isinstance(obj, dict):
        return None, "not a JSON object"
    if "nodes" in obj and "graph" in obj:
        return "pipeline", "has nodes+graph"
    keys = [k for k in obj if not k.startswith("_") and k != "$schema"]
    hits = sorted(set(keys) & _ROOT_KEYS)
    if len(hits) >= 2 and 2 * len(hits) >= len(keys):
        return "root", "root keys {}".format(hits)
    return None, "fragment by shape ({}/{} root keys)".format(len(hits), len(keys))


def check_snippet(s: Snippet) -> Result:
    if s.marked_skip:
        return Result(s, "skip", "doc-snippet marker")
    try:
        obj = json.loads(strip_jsonc(s.body) if s.lang == "jsonc" else s.body)
    except ValueError as e:
        return Result(s, "skip", "not valid JSON ({}) — fragment".format(e))
    kind, why = classify(obj)
    if kind is None:
        return Result(s, "skip", why)
    try:
        if kind == "pipeline":
            validate_pipeline(obj)
        else:
            validate_root(obj)
        return Result(s, "pass", kind)
    except ValueError as e:
        return Result(s, "fail", "{}: {}".format(kind, e))


def doc_files() -> List[str]:
    pattern = os.path.join(ROOT, "docs", "**", "*.md")
    paths = sorted(glob.glob(pattern, recursive=True))
    for extra in ("README.md", "AGENTS.md"):
        p = os.path.join(ROOT, extra)
        if os.path.isfile(p):
            paths.append(p)
    return paths


def check_files(paths: List[str], root: str = ROOT) -> List[Result]:
    results: List[Result] = []
    for path in paths:
        rel = os.path.relpath(path, root)
        for s in iter_snippets(path, rel):
            results.append(check_snippet(s))
    return results


def report(results: List[Result], list_all: bool) -> int:
    fails = 0
    for r in results:
        if r.status == "fail":
            fails += 1
        if list_all or r.status == "fail":
            head = "{}:{} [{}] {}".format(r.snippet.path, r.snippet.line,
                                          r.snippet.lang, r.status.upper())
            print("{}  {}".format(head, r.detail.replace("\n", "\n    ")))
    counts = {"pass": 0, "skip": 0, "fail": 0}
    for r in results:
        counts[r.status] += 1
    print("checked {} snippet(s): {} pass, {} skip, {} FAIL".format(
        len(results), counts["pass"], counts["skip"], counts["fail"]))
    return 1 if fails else 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Validate JSON config snippets in the docs against the engine.")
    ap.add_argument("--list", action="store_true",
                    help="print every snippet and its fate (pass/skip/fail + why)")
    args = ap.parse_args(argv)
    return report(check_files(doc_files()), list_all=args.list)


if __name__ == "__main__":
    sys.exit(main())
