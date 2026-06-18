"""The `yaah` CLI: git-style subcommands translate to the same action spec the
legacy `yaah <root> --flag` form produces, and the legacy form still parses.

What it proves: `yaah run|list|resume|clear|explain|validate|trace …` map to the
right action without running anything, an unknown verb exits non-zero, and the old
flag syntax is untouched (back-compat). Usability-gaps #1.

Run: cd yaah && PYTHONPATH=src python3 tests/test_cli.py

Targets Python 3.9+.
"""
from __future__ import annotations

from yaah.runtime import _parse_cli, _parse_subcommand


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
    _expect(_parse_subcommand(["init", "mypipeline"]), action="init",
            target_dir="mypipeline")
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
                ["baton-schema"], ["baton-schema", R], ["baton-schema", R, "b1", "extra"]):
        try:
            _parse_subcommand(bad)
        except SystemExit as e:
            assert e.code == 2, (bad, e.code)
        else:
            raise AssertionError("expected SystemExit for {!r}".format(bad))

    print("PASS yaah subcommands map correctly; legacy form intact; bad input exits 2")


if __name__ == "__main__":
    main()
