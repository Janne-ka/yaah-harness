"""Run the script-style yaah test suite — one process per tests/test_*.py.

Used by: the pixi `test` task, CI, and any dev who wants one command instead of
the hand-rolled shell loop. Each test is a self-contained script with an
`if __name__ == "__main__"` runner; this executes them all and aggregates
PASS/FAIL, exiting nonzero if any failed. Sets PYTHONPATH=src so it works whether
or not yaah is installed (editable in a pixi env, or raw source in CI).

Run: python scripts/run_tests.py    (from the yaah/ directory)

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


def main() -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    tests = sorted(glob.glob(os.path.join(ROOT, "tests", "test_*.py")))
    passed, failed = 0, []
    for t in tests:
        r = subprocess.run([sys.executable, t], cwd=ROOT, env=env,
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
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
