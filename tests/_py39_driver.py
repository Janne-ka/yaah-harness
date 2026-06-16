"""Driver run UNDER the floor interpreter (python3.9) by test_py39_import_guard:
imports every module named on stdin (one per line) and reports failures.
A ModuleNotFoundError for a THIRD-PARTY package (optional dep not installed for
3.9) is a SKIP; anything else — SyntaxError, the PEP-604 TypeError at import
(the BUG-662 class), AttributeError — is a FAIL. Targets Python 3.9+ (it IS
the 3.9 check)."""
import importlib
import sys

fails = []
skips = []
for line in sys.stdin.read().splitlines():
    mod = line.strip()
    if not mod:
        continue
    try:
        importlib.import_module(mod)
    except ModuleNotFoundError as e:
        root = (e.name or "").split(".")[0]
        if root and not mod.startswith(root):
            skips.append("{} (missing third-party {!r})".format(mod, e.name))
        else:
            fails.append("{}: {}".format(mod, e))
    except Exception as e:  # noqa: BLE001 — every other import-time error is the point
        fails.append("{}: {}: {}".format(mod, type(e).__name__, e))

for s in skips:
    print("SKIP " + s)
for f in fails:
    print("FAIL " + f)
print("py39-import: {} failed, {} skipped".format(len(fails), len(skips)))
sys.exit(1 if fails else 0)
