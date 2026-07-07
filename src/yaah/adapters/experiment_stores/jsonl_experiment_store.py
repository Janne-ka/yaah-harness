"""JsonlExperimentStore — one append-only rows.jsonl per experiment (AB-0).

Used by: `yaah ab` as the default row substrate — dev/CI-friendly (a campaign
is a file you can cat, diff, and commit), deterministic, zero-dependency.
Where: yaah.adapters.experiment_stores; implements yaah.experiment.ExperimentStore.
Why JSONL-per-experiment and not the K/V StoreBackend: rows are an append
stream; the engine's own precedent for append-only telemetry is the JSONL
trace sink ("append-only … crash-safe"). Each append is write+flush+fsync so
an acknowledged row survives the process dying on the next line.

Production campaigns wanting database reliability swap in the Postgres
adapter — same port, INSERT-only.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List
from urllib.parse import quote

from ...experiment import ExperimentStore


class JsonlExperimentStore(ExperimentStore):
    """Rows for experiment X live in <dir>/<quote(X)>.rows.jsonl."""

    def __init__(self, base_dir: str) -> None:
        self._dir = base_dir
        os.makedirs(base_dir, exist_ok=True)

    def _path(self, experiment_id: str) -> str:
        # quote(safe="") — an experiment id must never traverse paths
        return os.path.join(self._dir, quote(experiment_id, safe="") + ".rows.jsonl")

    async def append_row(self, experiment_id: str, row: Dict[str, Any]) -> None:
        line = json.dumps(row, sort_keys=True)
        with open(self._path(experiment_id), "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())   # acknowledged = durable, per the port contract

    async def rows(self, experiment_id: str) -> List[Dict[str, Any]]:
        path = self._path(experiment_id)
        if not os.path.exists(path):
            return []   # campaign not started — empty, not an error
        out: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as f:
            for n, line in enumerate(f, start=1):
                if not line.strip():
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError as e:
                    # LOUD: silently skipping a torn row undercounts the
                    # population — the exact reliability failure this store
                    # exists to prevent. Name the spot; the operator decides.
                    raise ValueError(
                        "corrupt experiment row at {} line {} (experiment {!r}): {} — "
                        "repair or remove the line; rows before it are intact".format(
                            path, n, experiment_id, e)) from e
        return out
