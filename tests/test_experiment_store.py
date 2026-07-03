"""ExperimentStore — the append-shaped rows port behind `yaah ab` (AB-0).

The reliability contract under test: every appended row survives (append-only,
flushed per row, crash-safe by shape); rows come back in order, per experiment;
corruption is LOUD (a torn/garbage line names the file + line number instead of
silently undercounting a campaign's population); experiments are isolated.

Run: cd yaah && PYTHONPATH=src python3 tests/test_experiment_store.py

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

from yaah.adapters.experiment_stores import JsonlExperimentStore
from yaah.experiment import ExperimentStore


def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        store = JsonlExperimentStore(d)
        assert isinstance(store, ExperimentStore)   # structural conformance

        # rows append and come back in order, per experiment, isolated
        asyncio.run(store.append_row("exp-a", {"variant": "A", "rep": 0}))
        asyncio.run(store.append_row("exp-a", {"variant": "B", "rep": 0}))
        asyncio.run(store.append_row("exp-b", {"variant": "X", "rep": 0}))
        rows_a = asyncio.run(store.rows("exp-a"))
        assert [r["variant"] for r in rows_a] == ["A", "B"], rows_a
        assert asyncio.run(store.rows("exp-b"))[0]["variant"] == "X"
        assert asyncio.run(store.rows("exp-missing")) == []   # no rows yet = empty, not an error

        # rows survive a NEW store instance over the same dir (durability)
        again = JsonlExperimentStore(d)
        assert len(asyncio.run(again.rows("exp-a"))) == 2

        # a corrupt line is LOUD, naming file + line — silent skipping would
        # undercount a campaign's population, the exact reliability failure
        # this store exists to prevent
        path = [os.path.join(d, f) for f in os.listdir(d) if "exp-a" in f][0]
        with open(path, "a") as f:
            f.write("{torn write\n")
        try:
            asyncio.run(again.rows("exp-a"))
            raise AssertionError("corrupt row line must be loud")
        except ValueError as e:
            assert "line 3" in str(e) and "exp-a" in str(e), e

        # experiment ids that are hostile as filenames are quoted, not path-injected
        asyncio.run(store.append_row("../evil/exp", {"ok": 1}))
        assert not os.path.exists(os.path.join(os.path.dirname(d), "evil"))
        assert asyncio.run(store.rows("../evil/exp")) == [{"ok": 1}]

    print("PASS ExperimentStore: append/order/isolation/durability/loud-corruption/quoting")


if __name__ == "__main__":
    main()
