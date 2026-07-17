"""Tests that `python -m yaah` delegates to the same entry as `yaah.cli:main`.

Run: cd yaah && PYTHONPATH=src python3 tests/test_main_module.py

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import io
import runpy
import sys


def scenario_module_output_matches_cli() -> None:
    """Executing `python -m yaah` (via runpy) and calling cli.main() directly must
    produce the same stdout and exit code — the __main__.py delegates, it does not
    duplicate."""
    from yaah.cli import main as cli_main

    def _capture(fn) -> tuple:
        old_argv, old_stdout = sys.argv, sys.stdout
        sys.argv = ["yaah"]
        sys.stdout = io.StringIO()
        try:
            try:
                fn()
                code = 0
            except SystemExit as e:
                code = 0 if e.code is None else int(e.code)
            return code, sys.stdout.getvalue()
        finally:
            sys.argv, sys.stdout = old_argv, old_stdout

    code_cli, out_cli = _capture(cli_main)

    def _run_module():
        runpy.run_module("yaah", run_name="__main__", alter_sys=False)

    code_mod, out_mod = _capture(_run_module)

    assert code_cli == code_mod, (
        "exit codes differ: cli={} module={}".format(code_cli, code_mod))
    assert out_cli == out_mod, (
        "output differs:\ncli:    {!r}\nmodule: {!r}".format(out_cli, out_mod))


async def main() -> None:
    scenario_module_output_matches_cli()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
