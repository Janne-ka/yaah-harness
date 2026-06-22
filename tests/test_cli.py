"""The `yaah` CLI: git-style subcommands translate to the same action spec the
legacy `yaah <root> --flag` form produces, and the legacy form still parses.

What it proves: `yaah run|list|resume|clear|explain|validate|trace …` map to the
right action without running anything, an unknown verb exits non-zero, and the old
flag syntax is untouched (back-compat). Usability-gaps #1.

Run: cd yaah && PYTHONPATH=src python3 tests/test_cli.py

Targets Python 3.9+.
"""
from __future__ import annotations

import io
import sys

from yaah.cli import _resolve_version, main as cli_main
from yaah.runtime import _parse_cli, _parse_subcommand


def _run_cli(argv: list) -> tuple:
    """Drive cli.main() with a fake argv and capture (exit_code, stdout). Used to
    cover the top-level affordances (`yaah --version`, bare `yaah`) that are
    handled before subcommand dispatch — _parse_cli / _parse_subcommand never
    see them, so the unit-level test grammar above doesn't reach them."""
    old_argv, old_stdout = sys.argv, sys.stdout
    sys.argv = ["yaah"] + argv
    sys.stdout = io.StringIO()
    try:
        try:
            cli_main()
            code = 0
        except SystemExit as e:
            code = 0 if e.code is None else int(e.code)
        return code, sys.stdout.getvalue()
    finally:
        sys.argv, sys.stdout = old_argv, old_stdout


def _validate_pipeline_extension_smoke() -> None:
    """Audit-derived regression: a root pointing at a pipeline file with an
    unresolved graph target must now fail `yaah validate` (was: 'ok'). Also
    verifies the new success message names BOTH files when validation passes,
    so a future regression that silently skips pipeline-loading is caught."""
    import json
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        # Bad: pipeline references a non-existent stage as `then`.
        bad_pl = os.path.join(td, "bad.pipeline.json")
        with open(bad_pl, "w") as f:
            json.dump({"nodes": {"a": {"type": "agent", "model": "fake:m",
                                        "template": "x"}},
                       "graph": {"start": "s", "stages":
                                 {"s": {"node": "a", "then": "no_such"}}}}, f)
        bad_root = os.path.join(td, "bad.root.json")
        with open(bad_root, "w") as f:
            json.dump({"transport": {"type": "inproc"},
                       "state": {"type": "memory"},
                       "providers": {"fake": {"type": "fake_scripted"}},
                       "default_provider": "fake",
                       "pipeline": "bad.pipeline.json"}, f)
        # stderr is what cli.main() prints errors to; we capture it like stdout.
        old_argv, old_stderr = sys.argv, sys.stderr
        sys.argv = ["yaah", "validate", bad_root]
        sys.stderr = io.StringIO()
        try:
            try:
                cli_main()
                code = 0
            except SystemExit as e:
                code = 0 if e.code is None else int(e.code)
            err = sys.stderr.getvalue()
        finally:
            sys.argv, sys.stderr = old_argv, old_stderr
        assert code == 2, (code, err)
        assert "invalid pipeline" in err, err
        assert "no_such" in err, err

        # Good: a minimal-but-valid pipeline. Validates clean and the success
        # message names both files (the contract this scenario locks).
        ok_pl = os.path.join(td, "ok.pipeline.json")
        with open(ok_pl, "w") as f:
            json.dump({"nodes": {"a": {"type": "agent", "model": "fake:m",
                                        "template": "x"}},
                       "graph": {"start": "s", "stages":
                                 {"s": {"node": "a"}}}}, f)
        ok_root = os.path.join(td, "ok.root.json")
        with open(ok_root, "w") as f:
            json.dump({"transport": {"type": "inproc"},
                       "state": {"type": "memory"},
                       "providers": {"fake": {"type": "fake_scripted"}},
                       "default_provider": "fake",
                       "pipeline": "ok.pipeline.json"}, f)
        code, out = _run_cli(["validate", ok_root])
        assert code == 0, (code, out)
        assert "root + pipeline" in out and "ok.pipeline.json" in out, out


def _expect(spec: dict, **want) -> None:
    for k, v in want.items():
        assert spec.get(k) == v, "{}: got {!r}, want {!r} (spec={})".format(k, spec.get(k), v, spec)


def main() -> None:
    R = "root.json"

    _expect(_parse_subcommand(["run", R]), action="run", root=R)
    _expect(_parse_subcommand(["run", R, "--fake"]), action="run", root=R, fake=True)
    _expect(_parse_subcommand(["list", R]), action="list", root=R, json=False)
    _expect(_parse_subcommand(["list", R, "--json"]), action="list", root=R, json=True)
    # --json on the legacy form too
    _expect(_parse_cli([R, "--list", "--json"]), action="list", root=R, json=True)
    # --json on a non-list action is an error
    for bad in (["list", R, "--json", "--list"],):  # noise after the json flag
        try:
            _parse_subcommand(bad)
        except SystemExit as e:
            assert e.code == 2, (bad, e.code)
        else:
            raise AssertionError("expected SystemExit for {!r}".format(bad))
    for bad_legacy in ([R, "--json", "--resume", "b1"], [R, "--json"]):
        try:
            _parse_cli(bad_legacy)
        except SystemExit as e:
            assert e.code == 2, (bad_legacy, e.code)
        else:
            raise AssertionError("expected SystemExit for {!r}".format(bad_legacy))
    _expect(_parse_subcommand(["clear", R]), action="clear", root=R)
    _expect(_parse_subcommand(["explain", R]), action="explain", root=R)
    _expect(_parse_subcommand(["resume", R, "b1"]), action="resume", root=R,
            baton_id="b1", decision_file=None)
    _expect(_parse_subcommand(["resume", R, "b1", "dec.json"]), action="resume",
            root=R, baton_id="b1", decision_file="dec.json")
    _expect(_parse_subcommand(["validate", R]), action="validate", root=R)
    _expect(_parse_subcommand(["validate", R, "--fake"]), action="validate", fake=True)
    _expect(_parse_subcommand(["trace", "t.jsonl"]), action="trace",
            trace_path="t.jsonl", price_map=None)
    _expect(_parse_subcommand(["trace", "t.jsonl", "prices.json"]), action="trace",
            trace_path="t.jsonl", price_map="prices.json")
    # init is a back-compat alias for `scaffold linear` — same action.
    _expect(_parse_subcommand(["init", "mypipeline"]), action="scaffold",
            target_dir="mypipeline", archetype="linear")
    # scaffold takes an explicit archetype name first.
    _expect(_parse_subcommand(["scaffold", "linear", "mypipeline"]),
            action="scaffold", target_dir="mypipeline", archetype="linear")
    _expect(_parse_subcommand(["scaffold", "branch-with-gate", "mypipeline"]),
            action="scaffold", target_dir="mypipeline", archetype="branch-with-gate")
    _expect(_parse_subcommand(["scaffold", "fork-fanin", "mypipeline"]),
            action="scaffold", target_dir="mypipeline", archetype="fork-fanin")
    _expect(_parse_subcommand(["baton-schema", R, "b1"]), action="baton-schema",
            root=R, baton_id="b1")

    # legacy flag form is untouched
    _expect(_parse_cli([R]), action="run", root=R)
    _expect(_parse_cli([R, "--list"]), action="list", root=R)
    _expect(_parse_cli([R, "--resume", "b1", "dec.json"]), action="resume",
            baton_id="b1", decision_file="dec.json")

    # unknown verb / missing args exit non-zero
    for bad in (["bogus", R], ["resume", R], ["list"], ["trace"],
                ["init"], ["init", "a", "b"],
                ["scaffold"], ["scaffold", "linear"],
                ["scaffold", "linear", "a", "b"],
                ["baton-schema"], ["baton-schema", R], ["baton-schema", R, "b1", "extra"]):
        try:
            _parse_subcommand(bad)
        except SystemExit as e:
            assert e.code == 2, (bad, e.code)
        else:
            raise AssertionError("expected SystemExit for {!r}".format(bad))

    # Top-level affordances (handled by main() before subcommand dispatch).
    # --version / -V print a version line and exit 0.
    for v_argv in (["--version"], ["-V"]):
        code, out = _run_cli(v_argv)
        assert code == 0, (v_argv, code)
        assert out.startswith("yaah "), (v_argv, out)
    # Bare `yaah` and `yaah --help` / `-h` print help and exit 0 — no confusing
    # "missing root config" frame before the user has picked a command.
    for h_argv in ([], ["--help"], ["-h"]):
        code, out = _run_cli(h_argv)
        assert code == 0, (h_argv, code)
        assert "usage: yaah" in out and "error:" not in out, (h_argv, out)

    # Sanity: _resolve_version returns a non-empty string (either an installed
    # version or the source-checkout placeholder).
    v = _resolve_version()
    assert isinstance(v, str) and v, v

    # `yaah validate` now validates root + pipeline (closes the gap where the
    # old behavior pronounced "ok" while the referenced pipeline had unresolved
    # graph targets / missing types / etc.).
    _validate_pipeline_extension_smoke()

    print("PASS yaah subcommands map correctly; legacy form intact; bad input exits 2; --version / bare-yaah affordances")


if __name__ == "__main__":
    main()
