"""The `yaah baton-schema <root> <id>` CLI surface.

What it proves: an end-to-end round trip — save a Baton with a HumanGate-style
AWAIT envelope into a FileStore-backed BatonStore, then call `baton_schema`
with a root pointing at that dir and verify the emitted JSON carries the form's
schema + example + baton_id. Plus the error paths: unknown baton id, baton
with no `pending`, baton parked without a `form` (legacy gate).

Run: cd yaah && PYTHONPATH=src python3 tests/test_baton_schema_cli.py

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import io
import json
import shutil
import sys
import tempfile

from yaah.adapters.stores.file_store import FileStore
from yaah.core import Envelope, Kind
from yaah.harness.baton import Baton
from yaah.harness.baton_store import BatonStore
from yaah.runtime import baton_schema


def _root_for(state_dir: str) -> dict:
    return {"state": {"type": "file", "dir": state_dir}}


def _save_baton(state_dir: str, baton: Baton) -> None:
    asyncio.run(BatonStore(FileStore(state_dir)).save(baton))


def _capture(coro) -> tuple:
    buf, err_buf = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf, err_buf
    code = 0
    try:
        asyncio.run(coro)
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
        out, _, code = _capture(baton_schema(_root_for(tmp), tmp, "b-ok"))
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
        out2, _, _ = _capture(baton_schema(_root_for(tmp), tmp, "b-grill"))
        got2 = json.loads(out2)
        assert got2["schema"] == inline and got2["form"] == "json_schema", got2

        # unknown baton -> exit 1
        _, err, code = _capture(baton_schema(_root_for(tmp), tmp, "missing"))
        assert code == 1 and "no baton" in err, (code, err)

        # baton with no `pending` envelope -> exit 1
        b3 = Baton(id="b-empty", stage=None, status="suspended", pending=None)
        _save_baton(tmp, b3)
        _, err, code = _capture(baton_schema(_root_for(tmp), tmp, "b-empty"))
        assert code == 1 and "no parked envelope" in err, (code, err)

        # baton with `pending` but no `form` declared -> exit 1 with the actionable hint
        b4 = Baton(id="b-noform", stage="x", status="suspended",
                   pending=Envelope(Kind.AWAIT, {"ask": "legacy", "awaiting": "human"}))
        _save_baton(tmp, b4)
        _, err, code = _capture(baton_schema(_root_for(tmp), tmp, "b-noform"))
        assert code == 1 and "declared form" in err and "form:" in err, (code, err)

        print("PASS yaah baton-schema: end-to-end happy + 3 error paths")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
