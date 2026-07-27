"""`yaah validate` terminal rendering — multi-item advisory classes COLLAPSE to a
single count line by default (so the wall of node names can't bury the
`ok: ... is valid` verdict), and `--verbose` restores the full per-item listing.

Pins the D2 UX fix (mailbox M21): default is a count line + verdict LAST; verbose
lists names; `--strict` exit semantics are unchanged (advisories still fail with
exit 2). The collapse itself (`collapse_lint_warnings`) is unit-tested for the
second collapsible class (untrusted-unfenced) and the single-item passthrough,
so the generalization is pinned without building a fragile trigger config.

Run: PYTHONPATH=src python3 tests/test_validate_verbose.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ENV = {**os.environ, "PYTHONPATH": "src"}


def _big_advisory_root(d, n=5):
    """A pipeline with N undeclared envelope-transforms, each feeding a consumer —
    the exact shape that makes `transform-provides-undeclared` name many nodes."""
    nodes, stages = {}, {}
    start, prev = None, None
    for i in range(1, n + 1):
        a, t, r = "a%d" % i, "t%d" % i, "r%d" % i
        nodes[a] = {"type": "agent", "parse": False}
        nodes[t] = {"type": "transform", "target": "fn:m:f", "call": "envelope"}
        nodes[r] = {"type": "render", "template_text": "{{verdict%d}}" % i}
        sa, st, sr = "sa%d" % i, "st%d" % i, "sr%d" % i
        if start is None:
            start = sa
        stages[sa] = {"node": a, "then": st}
        stages[st] = {"node": t, "then": sr}
        stages[sr] = {"node": r, "then": None}
        if prev is not None:
            stages[prev]["then"] = sa
        prev = sr
    pipe = {"nodes": nodes, "graph": {"start": start, "stages": stages}}
    with open(os.path.join(d, "p.json"), "w") as f:
        json.dump(pipe, f)
    root = os.path.join(d, "root.json")
    with open(root, "w") as f:
        json.dump({"pipeline": "p.json"}, f)
    return root


def _run(root, *flags, combine=False):
    err = subprocess.STDOUT if combine else subprocess.PIPE
    r = subprocess.run([sys.executable, "-m", "yaah.cli", "validate", root, *flags],
                       stdout=subprocess.PIPE, stderr=err, text=True, env=ENV)
    return r.returncode, r.stdout, ("" if combine else r.stderr)


def cli_collapse_and_verbose() -> None:
    d = tempfile.mkdtemp()
    root = _big_advisory_root(d, n=5)

    # DEFAULT: one count line, no node names, verdict still printed, exit 0.
    rc, out, err = _run(root)
    assert rc == 0, (rc, out, err)
    assert "transform-provides-undeclared" in err, err
    assert "5 envelope-transform(s) don't declare" in err, err
    assert "run with --verbose to list them" in err, err
    assert "'st1'" not in err and "'st5'" not in err, err   # names collapsed away
    assert "is valid" in out, out

    # VERBOSE: the full listing returns — every node named, verdict still there.
    rc, out, err = _run(root, "--verbose")
    assert rc == 0, (rc, out, err)
    assert "'st1'" in err and "'st5'" in err, err
    assert "--verbose" not in err, err                       # not the collapse hint
    assert "is valid" in out, out

    # VERDICT LAST: with streams combined in true print order, the last non-empty
    # line is the verdict — the operator's eye lands on it, not the advisory.
    rc, combined, _ = _run(root, combine=True)
    lines = [ln for ln in combined.splitlines() if ln.strip()]
    assert lines[-1].startswith("ok:") and "is valid" in lines[-1], combined
    assert any("transform-provides-undeclared" in ln for ln in lines[:-1]), combined


def cli_strict_semantics_unchanged() -> None:
    d = tempfile.mkdtemp()
    root = _big_advisory_root(d, n=5)
    # --strict still fails on ANY advisory (exit 2), and the count line stays
    # collapsed by default (strict does not force the full listing).
    rc, out, err = _run(root, "--strict")
    assert rc == 2, (rc, out, err)
    assert "5 envelope-transform(s) don't declare" in err, err
    assert "'st1'" not in err, err
    assert "failing with exit 2" in err, err
    # --strict --verbose: still exit 2, but now the full listing is shown.
    rc, out, err = _run(root, "--strict", "--verbose")
    assert rc == 2, (rc, out, err)
    assert "'st1'" in err, err


def unit_collapse_generalizes_and_preserves() -> None:
    sys.path.insert(0, "src")
    from yaah.validate import collapse_lint_warnings

    # untrusted-unfenced: one warning per site -> collapses on count>=2.
    untrusted = [
        "stage 'g1': the human_gate interpolates {{x}} UNFENCED [lint: untrusted-unfenced]",
        "stage 'g2': the render interpolates {{y}} UNFENCED [lint: untrusted-unfenced]",
        "stage 'g3': the render interpolates {{z}} UNFENCED [lint: untrusted-unfenced]",
    ]
    collapsed = collapse_lint_warnings(untrusted, verbose=False)
    assert len(collapsed) == 1, collapsed
    wid, msg = collapsed[0]
    assert wid == "untrusted-unfenced" and "3 template site(s)" in msg, collapsed
    assert "g1" not in msg and "g3" not in msg, collapsed
    # verbose restores every site, in order.
    full = collapse_lint_warnings(untrusted, verbose=True)
    assert len(full) == 3 and all(w == "untrusted-unfenced" for w, _ in full), full

    # SINGLE-ITEM stays as-is (both collapsible classes and unknown ones).
    one_site = [untrusted[0]]
    assert collapse_lint_warnings(one_site, verbose=False) == \
        collapse_lint_warnings(one_site, verbose=True), "single site must not collapse"
    single_transform = ["envelope-transform(s) 't1' don't declare `provides`, "
                        "so the lint ... [lint: transform-provides-undeclared]"]
    got = collapse_lint_warnings(single_transform, verbose=False)
    assert len(got) == 1 and "'t1'" in got[0][1], got   # one transform reads fine as-is
    unknown = ["stage 'x': something odd [lint: some-other-rule]",
               "plain warning with no trailer"]
    got = collapse_lint_warnings(unknown, verbose=False)
    assert got == [("some-other-rule", "stage 'x': something odd"),
                   (None, "plain warning with no trailer")], got


def main() -> None:
    cli_collapse_and_verbose()
    cli_strict_semantics_unchanged()
    unit_collapse_generalizes_and_preserves()
    print("ok")


if __name__ == "__main__":
    main()
