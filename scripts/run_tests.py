"""Run the script-style yaah test suite — one process per tests/test_*.py.

Used by: the pixi `test` task, CI, and any dev who wants one command instead of
the hand-rolled shell loop. Each test is a self-contained script with an
`if __name__ == "__main__"` runner; this executes them all and aggregates
PASS/FAIL, exiting nonzero if any failed. Sets PYTHONPATH=src so it works whether
or not yaah is installed (editable in a pixi env, or raw source in CI).

Coverage floor (HARD GATE, 2026-06-22): each test runs under `coverage run -p`
and the combined report enforces `[tool.coverage.report] fail_under` in
pyproject.toml (75%) — the command exits nonzero if coverage dips below it
OR if the `coverage` package isn't installed. The "silently pass when
coverage isn't installed" loophole was closed because it let CI/commit gates
slip below the floor unnoticed.

Pass `--no-coverage` for a fast LOCAL DEV LOOP only — it bypasses the
gate entirely and is not safe for CI or pre-commit. `--coverage` is the
explicit opt-in (kept for symmetry / readability) but matches the default.

mypy RATCHET: when mypy is installed, the error count must not exceed
scripts/mypy_baseline.txt. A ratchet (not zero-errors) so the gate is real
TODAY while the legacy `Any`-seam tail is paid down batch by batch — lower
the baseline as errors are fixed, never raise it. mypy missing = a NOTE,
not a failure (the suite stays runnable offline with zero deps).

Run: python scripts/run_tests.py               (from the yaah/ directory)
     python scripts/run_tests.py --no-coverage  (dev loop, skips the gate)

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


def _mypy_ratchet(env: dict) -> int:
    """Contract gate: mypy's error count must not EXCEED the recorded baseline
    (scripts/mypy_baseline.txt). Returns 0 ok / 1 regression. Skips with a note
    when mypy isn't installed — the ratchet is for envs that have the dev deps."""
    try:
        import mypy  # noqa: F401
    except Exception:
        print("NOTE: mypy not installed — contract ratchet skipped "
              "(pip install mypy to enforce it).")
        return 0
    baseline_file = os.path.join(HERE, "mypy_baseline.txt")
    baseline = int(open(baseline_file).read().strip())
    r = subprocess.run([sys.executable, "-m", "mypy"], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    count = sum(1 for line in r.stdout.splitlines() if ": error:" in line)
    if count > baseline:
        print("FAIL: mypy errors went {} -> {} (baseline {}). New type errors:"
              .format(baseline, count, baseline_file))
        print(r.stdout[-3000:])
        return 1
    if count < baseline:
        print("mypy: {} errors (< baseline {}) — ratchet down: echo {} > {}"
              .format(count, baseline, count, os.path.relpath(baseline_file, ROOT)))
    else:
        print("mypy: {} errors (= baseline, ok)".format(count))
    return 0


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
    # HARD GATE (2026-06-22): the default mode REQUIRES coverage. If `coverage`
    # isn't installed AND --no-coverage wasn't passed, fail loudly — silent
    # bypass was letting the floor slip unnoticed.
    if not no_cov and not _coverage_available():
        print("ERROR: the 'coverage' package is not installed and --no-coverage "
              "was not passed. Either install it (pip install 'coverage[toml]') "
              "for the enforced gate, or pass --no-coverage for a dev-loop run "
              "that skips the floor check.", file=sys.stderr)
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

    if _mypy_ratchet(env) != 0:
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
