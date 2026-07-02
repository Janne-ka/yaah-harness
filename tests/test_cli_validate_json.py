"""`yaah validate --json` — ONE machine-readable diagnostics object on stdout,
for the generate->validate->repair loop (an LLM author patches from `errors`/
`warnings` without prose parsing). Exit codes match prose mode: 0/1/2.

Run: cd yaah && PYTHONPATH=src python3 tests/test_cli_validate_json.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ENV = {**os.environ, "PYTHONPATH": "src"}


def _run(d, name, *flags):
    r = subprocess.run([sys.executable, "-m", "yaah.cli", "validate",
                        os.path.join(d, name), *flags],
                       capture_output=True, text=True, env=ENV)
    return r.returncode, r.stdout, r.stderr


def main() -> None:
    d = tempfile.mkdtemp()
    pipe = {"nodes": {"x": {"type": "shell", "argv": ["true"]}},
            "graph": {"start": "s", "stages": {"s": {"node": "x", "then": None}}}}
    with open(os.path.join(d, "p.json"), "w") as f:
        json.dump(pipe, f)
    with open(os.path.join(d, "ok.json"), "w") as f:
        json.dump({"pipeline": "p.json"}, f)

    rc, out, _ = _run(d, "ok.json", "--json")
    o = json.loads(out)   # stdout is EXACTLY one JSON object
    assert rc == 0 and o["ok"] and o["errors"] == [] and o["warnings"] == [], (rc, o)

    # a root typo becomes a diagnostic + exit 1 (not a traceback)
    with open(os.path.join(d, "bad.json"), "w") as f:
        json.dump({"pipeline": "p.json", "transprt": {"type": "inproc"}}, f)
    rc, out, _ = _run(d, "bad.json", "--json")
    o = json.loads(out)
    assert rc == 1 and not o["ok"], (rc, o)
    assert any("transprt" in e["message"] and "transport" in e["message"]
               for e in o["errors"]), o   # did-you-mean survives into the diagnostic

    # a stage-scoped error carries the stage field (best-effort path info)
    p2 = {"nodes": {"x": {"type": "shell", "argv": ["true"]}},
          "graph": {"start": "s",
                    "stages": {"s": {"node": "x", "on_error": "claer", "then": None}}}}
    with open(os.path.join(d, "p2.json"), "w") as f:
        json.dump(p2, f)
    with open(os.path.join(d, "bad2.json"), "w") as f:
        json.dump({"pipeline": "p2.json"}, f)
    rc, out, _ = _run(d, "bad2.json", "--json")
    o = json.loads(out)
    assert rc == 1 and o["errors"][0].get("stage") == "s", o

    # prose mode unchanged: same bad config still exits nonzero without --json
    rc, out, err = _run(d, "bad2.json")
    assert rc != 0 and "claer" in (out + err), (rc, out, err)

    # the shared seam both surfaces consume (validate.validate_config +
    # split_lint_id): a valid root with NO pipeline yields zero warnings, and
    # a warning without the "[lint: id]" trailer splits to (None, itself).
    sys.path.insert(0, "src")
    from yaah.validate import split_lint_id, validate_config
    assert validate_config({"state": {"type": "memory"}}, d) == []
    assert split_lint_id("plain text warning") == (None, "plain text warning")
    wid, msg = split_lint_id("stage 'x': weak [lint: weak-output-schema]")
    assert wid == "weak-output-schema" and msg == "stage 'x': weak", (wid, msg)
    print("ok")


if __name__ == "__main__":
    main()
