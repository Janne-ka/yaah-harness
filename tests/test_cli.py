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


def _cli_dispatch_path_coverage() -> None:
    """Integration tests for the dispatch arms this batch added or modified.
    Not coverage padding — each scenario locks in user-visible CLI behavior
    that the unit tests in test_doctor.py / test_trace_pretty.py don't reach
    (they exercise the pure functions; this exercises the cli.main() wiring,
    the new flag parsers, and the new operator-visible output). Trims any
    "test exists because the line exists" cases."""
    import json
    import os
    import tempfile

    # 1. doctor dispatch — verifies the main()→_dispatch wiring for the new
    # verb. diagnose() itself is unit-tested in test_doctor.py; this catches
    # a missing/misnamed dispatch arm or a dropped SystemExit code.
    code, out = _run_cli(["doctor"])
    assert code == 0, (code, out)
    assert "DOCTOR: ok" in out, out

    # 2. trace dispatch — aggregate (default), --cost, --pretty, --errors-only,
    # --last. All four flags are new across batches 1-3; the unit logic is
    # tested in test_trace_pretty.py — this asserts the CLI wires each flag
    # to the right function.
    with tempfile.TemporaryDirectory() as td:
        jsonl = os.path.join(td, "t.jsonl")
        with open(jsonl, "w") as f:
            for rec in [
                {"id": "s1", "corr": "a", "name": "stage", "parent": "p",
                 "duration_ms": 100.0, "status": "ok", "stage": "draft"},
                {"id": "m1", "corr": "a", "name": "model_call", "parent": "s1",
                 "duration_ms": 95.0, "tokens_in": 100, "tokens_out": 50,
                 "model": "fake:x"},
                {"id": "s2", "corr": "b", "name": "stage", "parent": "p",
                 "duration_ms": 50.0, "status": "error", "stage": "verify",
                 "error": "oops"},
            ]:
                f.write(json.dumps(rec) + "\n")
        # aggregate default — exits 0, JSON output
        code, out = _run_cli(["trace", jsonl])
        assert code == 0 and "totals" in out, (code, out)
        # --pretty
        code, out = _run_cli(["trace", jsonl, "--pretty"])
        assert code == 0 and "run a" in out and "errors:" in out, (code, out)
        # --cost
        code, out = _run_cli(["trace", jsonl, "--cost"])
        assert code == 0 and "1 model call" in out, (code, out)
        # --errors-only on a trace with errors -> exit 1
        code, out = _run_cli(["trace", jsonl, "--errors-only"])
        assert code == 1 and "oops" in out, (code, out)
        # --last 1 keeps only run b (which has the error)
        code, out = _run_cli(["trace", jsonl, "--pretty", "--last", "1"])
        assert code == 0 and "run b" in out and "run a" not in out, (code, out)

    # 3. trace flag validation — the error branches my new --last parser owns
    # plus the new view-flag mutex. Each one is a branch I authored this batch;
    # a regression would mean a malformed CLI silently does the wrong thing.
    bad_combos = [
        (["trace", "/nonexistent", "--last"],   "--last needs"),       # no N value
        (["trace", "/nonexistent", "--last", "abc"], "must be an integer"),
        (["trace", "/nonexistent", "--last", "-3"],  "must be positive"),
        (["trace", "/nonexistent", "--pretty", "--cost"], "mutually exclusive"),
        (["trace", "/nonexistent", "--pretty", "--errors-only", "--cost"], "mutually exclusive"),
    ]
    for argv, needle in bad_combos:
        old_argv, old_stderr = sys.argv, sys.stderr
        sys.argv = ["yaah"] + argv
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
        assert code == 2, (argv, code, err)
        assert needle in err, (argv, err)

    # 4. scaffold happy path — locks in the new two-line `Next:` / `Then:`
    # hint added this batch. The scaffold function itself has its own tests;
    # this asserts cli wiring + the post-create message the operator sees.
    with tempfile.TemporaryDirectory() as td:
        target = os.path.join(td, "newpipeline")
        code, out = _run_cli(["scaffold", "linear", target])
        assert code == 0, (code, out)
        assert "Next:" in out and "Then:" in out, out

    # 5. scaffold --list and init --list — new discovery affordance. The
    # output must name each archetype + its one-line description (otherwise
    # users see an empty list, defeating the point of the verb).
    code, out = _run_cli(["scaffold", "--list"])
    assert code == 0, (code, out)
    assert "linear" in out and "fork-fanin" in out, out
    assert "Use: yaah scaffold" in out, out
    # init --list is an alias for scaffold --list (so first-timers discover
    # via the verb they typed). Same dispatch -> same output.
    code, out2 = _run_cli(["init", "--list"])
    assert code == 0, (code, out2)
    assert out == out2, "init --list should mirror scaffold --list"

    # 6. completion dispatch via main() — verifies the new verb wires through
    # to the render() function and prints the expected shell-specific marker.
    code, out = _run_cli(["completion", "bash"])
    assert code == 0, (code, out)
    assert "complete -F _yaah_completion yaah" in out, out
    code, out = _run_cli(["completion", "zsh"])
    assert code == 0, (code, out)
    assert "#compdef yaah" in out, out
    # Bad shell name -> exit 2 via the ValueError boundary in main().
    code, out = _run_cli(["completion", "fish"])
    assert code == 2, (code, out)

    # 7. trace --corr through main() — new filter added this batch. A
    # matching corr keeps the run; a non-matching corr produces the
    # "(no records)" placeholder, exit 0 (it's not an error to ask for
    # a run that isn't in the trace).
    with tempfile.TemporaryDirectory() as td:
        jsonl = os.path.join(td, "t.jsonl")
        with open(jsonl, "w") as f:
            f.write(json.dumps({"id": "s1", "corr": "abc", "name": "stage",
                                 "parent": "p", "status": "ok",
                                 "duration_ms": 1.0, "stage": "x"}) + "\n")
            f.write(json.dumps({"id": "s2", "corr": "def", "name": "stage",
                                 "parent": "p", "status": "ok",
                                 "duration_ms": 1.0, "stage": "y"}) + "\n")
        code, out = _run_cli(["trace", jsonl, "--pretty", "--corr", "abc"])
        assert code == 0 and "run abc" in out and "run def" not in out, (code, out)
        code, out = _run_cli(["trace", jsonl, "--pretty", "--corr", "no-such"])
        assert code == 0 and "(no records)" in out, (code, out)


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


def _fn_module_resolves_from_config_dir() -> None:
    """A console-script `yaah run` resolves an example-local `fn:module:func`
    target relative to the CONFIG FILE's directory — not the cwd. This is the
    real-world bug: `python3 -m yaah.runtime` only worked because `-m` puts the
    cwd on sys.path; the installed `yaah` entry point doesn't, so an
    example-local `transforms.py` died with ModuleNotFoundError.

    We reproduce the console-script environment exactly: a temp dir holding
    `transforms.py` + a minimal `fn:transforms:*` pipeline, invoked via
    cli.main() with the temp dir NOT on sys.path and NOT as cwd. Before the fix
    the run dies in the transform stage with `No module named 'transforms'`;
    after, the engine front-inserts the config dir and the module resolves."""
    import json
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, "transforms.py"), "w") as f:
            f.write("def stamp(envelope, config):\n    return {'ok': True}\n")
        pl = os.path.join(td, "p.pipeline.json")
        with open(pl, "w") as f:
            json.dump({"nodes": {"t": {"type": "transform",
                                       "target": "fn:transforms:stamp",
                                       "call": "envelope"}},
                       "graph": {"start": "s", "stages": {"s": {"node": "t"}}}}, f)
        inp = os.path.join(td, "input.json")
        with open(inp, "w") as f:
            json.dump({}, f)
        root = os.path.join(td, "r.root.json")
        with open(root, "w") as f:
            json.dump({"transport": {"type": "inproc"},
                       "state": {"type": "memory"},
                       "pipeline": "p.pipeline.json",
                       "input": "input.json",
                       "run": True}, f)

        # Guard against false negatives: the config dir must NOT already be
        # reachable (no cwd shortcut, no leftover sys.path entry).
        assert td not in sys.path, td
        assert os.getcwd() != td, td

        old_argv, old_out, old_err = sys.argv, sys.stdout, sys.stderr
        sys.argv = ["yaah", "run", root]
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        try:
            try:
                cli_main()
                code = 0
            except SystemExit as e:
                code = 0 if e.code is None else int(e.code)
            err = sys.stdout.getvalue() + sys.stderr.getvalue()
        finally:
            sys.argv, sys.stdout, sys.stderr = old_argv, old_out, old_err
            # The production guard dedupes, but a test must not leak global state.
            # (line 270 asserted td wasn't on the path, so we own every entry.)
            while td in sys.path:
                sys.path.remove(td)
        # Pre-fix this is the exact failure: the run dies because the engine
        # never put the config dir on sys.path. Post-fix the module resolves.
        assert "No module named 'transforms'" not in err, err
        assert code == 0, (code, err)


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

    # Coverage for dispatch arms cli.py owns (doctor, scaffold, trace flag matrix).
    _cli_dispatch_path_coverage()

    # `fn:` modules resolve relative to the config dir (console-script parity).
    _fn_module_resolves_from_config_dir()

    print("PASS yaah subcommands map correctly; legacy form intact; bad input exits 2; --version / bare-yaah affordances")


if __name__ == "__main__":
    main()
