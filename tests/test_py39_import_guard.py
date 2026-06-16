"""Runtime-compat guard (BUG-662 root cause): every yaah + app module must
IMPORT under the Python 3.9 floor — a PEP-604 annotation evaluated at import
(`int | None` without `from __future__ import annotations`) is one line of
3.10+ syntax that compiles fine on dev machines and kills consumer envs; the
bash-era gate (test-all-factory-py-py39-import.sh) lived three weeks blind
without this. Class-level gate: discovers modules, imports them all under
python3.9 via tests/_py39_driver.py. SKIPS (exit 0, loudly) when no 3.9
interpreter exists on the host.

Run: cd yaah && PYTHONPATH=src python3 tests/test_py39_import_guard.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_YAAH_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))


def _modules() -> list:
    mods = []
    pkg_root = os.path.join(_YAAH_SRC, "yaah")
    for dirpath, dirnames, filenames in os.walk(pkg_root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for f in filenames:
            if not f.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, f), _YAAH_SRC)
            mod = rel[:-3].replace(os.sep, ".")
            if mod.endswith(".__init__"):
                mod = mod[: -len(".__init__")]
            mods.append(mod)
    return sorted(set(mods))


def main() -> None:
    py39 = shutil.which("python3.9") or os.environ.get("PY39", "")
    if sys.version_info[:2] == (3, 9):
        py39 = sys.executable
    if not py39 or not shutil.which(py39):
        print("SKIP: no python3.9 on this host — the 3.9 import floor is UNVERIFIED here")
        return
    mods = _modules()
    env = dict(os.environ, PYTHONPATH=_YAAH_SRC)
    r = subprocess.run([py39, os.path.join(_HERE, "_py39_driver.py")],
                       input="\n".join(mods), text=True, env=env,
                       capture_output=True, timeout=300)
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        raise SystemExit("py39 import guard FAILED ({} modules checked)".format(len(mods)))
    print("ok ({} modules import clean under {})".format(len(mods), py39))


if __name__ == "__main__":
    main()
