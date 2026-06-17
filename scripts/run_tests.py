"""Run the script-style yaah test suite — one process per tests/test_*.py.

Used by: the pixi `test` task, CI, and any dev who wants one command instead of
the hand-rolled shell loop. Each test is a self-contained script with an
`if __name__ == "__main__"` runner; this executes them all and aggregates
PASS/FAIL, exiting nonzero if any failed. Sets PYTHONPATH=src so it works whether
or not yaah is installed (editable in a pixi env, or raw source in CI).

Coverage floor: when the `coverage` package is importable, each test runs under
`coverage run -p` and the combined report enforces `[tool.coverage.report]
fail_under` in pyproject.toml (75%) — the command exits nonzero if coverage dips
below it. CI and the pixi env install coverage, so the floor gates every push.
Pass `--no-coverage` for a fast local loop; pass `--coverage` to require it
(errors if `coverage` is missing).

Run: python scripts/run_tests.py               (from the yaah/ directory)
     python scripts/run_tests.py --no-coverage

Targets Python 3.9+.
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)              # yaah/
SRC = os.path.join(ROOT, "src")


def _coverage_available() -> bool:
    try:
        import coverage  # noqa: F401
        return True
    except Exception:
        return False


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    force_cov, no_cov = "--coverage" in argv, "--no-coverage" in argv
    if force_cov and no_cov:
        print("ERROR: pass at most one of --coverage / --no-coverage",
              file=sys.stderr)
        return 2
    if force_cov and not _coverage_available():
        print("ERROR: --coverage requested but the 'coverage' package is not "
              "installed (pip install 'coverage[toml]').", file=sys.stderr)
        return 2
    use_cov = force_cov or (not no_cov and _coverage_available())

    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")

    if use_cov:
        subprocess.run([sys.executable, "-m", "coverage", "erase"],
                       cwd=ROOT, env=env)
        run_prefix = [sys.executable, "-m", "coverage", "run", "-p"]
    else:
        run_prefix = [sys.executable]

    tests = sorted(glob.glob(os.path.join(ROOT, "tests", "test_*.py")))
    passed, failed = 0, []
    for t in tests:
        r = subprocess.run(run_prefix + [t], cwd=ROOT, env=env,
                           capture_output=True, text=True)
        if r.returncode == 0:
            passed += 1
        else:
            failed.append(os.path.basename(t))
            print("FAIL: {}".format(os.path.basename(t)))
            print(r.stdout[-2000:])
            print(r.stderr[-2000:])
    print("PASS={} FAIL={}{}".format(
        passed, len(failed), (" " + " ".join(failed)) if failed else ""))
    if failed:
        return 1

    if not use_cov:
        print("NOTE: coverage not measured — install 'coverage[toml]' to enforce "
              "the fail_under floor in pyproject.toml.")
        return 0

    subprocess.run([sys.executable, "-m", "coverage", "combine"],
                   cwd=ROOT, env=env)
    rep = subprocess.run([sys.executable, "-m", "coverage", "report"],
                         cwd=ROOT, env=env)
    if rep.returncode != 0:
        print("FAIL: coverage is below the floor configured in pyproject.toml "
              "([tool.coverage.report] fail_under).")
        return rep.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
