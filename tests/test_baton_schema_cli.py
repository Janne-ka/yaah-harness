"""The `yaah baton-schema <root> <id>` CLI surface.

What it proves: the CLI dispatcher renders what the runtime action RETURNS —
the emitted JSON carries the form's schema + example + baton_id — and maps the
action's ValueError cases (unknown baton id, baton with no `pending`, baton
parked without a `form`) to `error: ...` on stderr with exit code 1, the
documented contract for driver skills. The action's own return/raise contract
is pinned in tests/test_runtime_actions.py; this file owns the rendering and
exit codes.

Run: cd yaah && PYTHONPATH=src python3 tests/test_baton_schema_cli.py

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
import tempfile

from yaah.adapters.stores.file_backend import FileBackend
from yaah.cli import _dispatch_baton_schema
from yaah.core import Envelope, Kind
from yaah.harness.baton import Baton
from yaah.harness.baton_store import BatonStore


def _root_for(state_dir: str) -> dict:
    return {"state": {"type": "file", "dir": state_dir}}


def _save_baton(state_dir: str, baton: Baton) -> None:
    asyncio.run(BatonStore(FileBackend(state_dir)).save(baton))


def _dispatch(state_dir: str, baton_id: str) -> tuple:
    """Run the CLI dispatcher, capturing (stdout, stderr, exit_code)."""
    buf, err_buf = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf, err_buf
    code = 0
    try:
        _dispatch_baton_schema({"baton_id": baton_id}, _root_for(state_dir), state_dir)
    except SystemExit as e:
        code = e.code or 0
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return buf.getvalue(), err_buf.getvalue(), code


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="yaah-baton-schema-")
    try:
        # happy path: built-in form
        pending = Envelope(Kind.AWAIT, {"ask": "ship?", "awaiting": "human:ship",
                                        "form": "approve_or_revise"})
        b = Baton(id="b-ok", stage="review", status="suspended",
                  awaiting="human:ship", pending=pending)
        _save_baton(tmp, b)
        out, _, code = _dispatch(tmp, "b-ok")
        assert code == 0
        got = json.loads(out)
        assert got["form"] == "approve_or_revise", got
        assert got["schema"]["properties"]["decision"]["enum"] == ["approve", "revise"]
        assert got["example"] == {"decision": "approve"}
        assert got["baton_id"] == "b-ok"
        assert got["awaiting"] == "human:ship"

        # escape hatch: inline schema is surfaced verbatim
        inline = {"type": "object", "properties": {"verdict": {"type": "string"}}}
        pending2 = Envelope(Kind.AWAIT, {"ask": "?", "awaiting": "human:grill",
                                         "form": "json_schema",
                                         "decision_schema": inline})
        b2 = Baton(id="b-grill", stage="grill", status="suspended",
                   awaiting="human:grill", pending=pending2)
        _save_baton(tmp, b2)
        out2, _, _ = _dispatch(tmp, "b-grill")
        got2 = json.loads(out2)
        assert got2["schema"] == inline and got2["form"] == "json_schema", got2

        # unknown baton -> exit 1
        _, err, code = _dispatch(tmp, "missing")
        assert code == 1 and "no baton" in err, (code, err)

        # baton with no `pending` envelope -> exit 1
        b3 = Baton(id="b-empty", stage=None, status="suspended", pending=None)
        _save_baton(tmp, b3)
        _, err, code = _dispatch(tmp, "b-empty")
        assert code == 1 and "no parked envelope" in err, (code, err)

        # baton with `pending` but no `form` declared -> exit 1 with the actionable hint
        b4 = Baton(id="b-noform", stage="x", status="suspended",
                   pending=Envelope(Kind.AWAIT, {"ask": "legacy", "awaiting": "human"}))
        _save_baton(tmp, b4)
        _, err, code = _dispatch(tmp, "b-noform")
        assert code == 1 and "declared form" in err and "form:" in err, (code, err)

        # a CORRUPTED store is a config-class failure, NOT a "wrong baton": the
        # store's JSONDecodeError (a ValueError subclass) must PROPAGATE to
        # main()'s exit-2 boundary, not be netted into the domain exit 1. The
        # dispatcher may only catch the action's own domain errors.
        b5 = Baton(id="b-corrupt", stage="x", status="suspended",
                   pending=Envelope(Kind.AWAIT, {"ask": "?", "awaiting": "human",
                                                 "form": "approve_or_revise"}))
        _save_baton(tmp, b5)
        # the backend's file name is quote("baton:<id>") + ".json"; its content
        # is a {rev, base64-value} record, so match on the NAME, not the content.
        corrupted = [n for n in os.listdir(tmp) if "b-corrupt" in n]
        assert corrupted, os.listdir(tmp)
        for name in corrupted:
            with open(os.path.join(tmp, name), "w") as f:
                f.write("{not json")
        try:
            _dispatch(tmp, "b-corrupt")
            raise AssertionError("corrupted store must not be swallowed as exit 1")
        except ValueError:
            pass   # propagates -> main() boundary renders it as exit 2

        print("PASS yaah baton-schema: end-to-end happy + 3 error paths + corrupt store")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
