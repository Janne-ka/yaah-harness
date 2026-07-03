"""ExperimentStore — the port for A/B experiment row collection (AB-0).

Used by: the `yaah ab` runner (writes one row per run) and the comparison
report (reads a campaign's rows back).
Where: yaah.experiment — the experiment layer's own port, NOT a facade over
StoreBackend: rows are append-only telemetry (the trace-JSONL precedent), and
a K/V read-modify-write per append is the wrong shape for a reliability-
critical append stream.
Why: the user's requirement is RELIABLE data collection across a campaign —
runs happen over days, from multiple invocations, possibly live; every run
must land a row (failures and parks included) and every row must survive.
The port keeps the substrate swappable: JSONL files for dev/CI (deterministic,
inspectable), a database (Postgres) for production campaigns.

Adapters: yaah.adapters.experiment_stores (jsonl today; postgres next).
Declare-your-port applies (ADR-0007): shipped adapters name this port in the
class header; one row in tests/test_ports.py.

Targets Python 3.9+.
"""
from __future__ import annotations

from abc import abstractmethod
from typing import Any, Dict, List, Protocol, runtime_checkable


@runtime_checkable
class ExperimentStore(Protocol):
    """Append-shaped row storage for one or more experiment campaigns."""

    @abstractmethod
    async def append_row(self, experiment_id: str, row: Dict[str, Any]) -> None:
        """Durably append one row to the experiment's stream. MUST be flushed
        before returning — a row acknowledged is a row that survives."""
        ...

    @abstractmethod
    async def rows(self, experiment_id: str) -> List[Dict[str, Any]]:
        """Every row appended to the experiment, in append order. An unknown
        experiment id is an empty list (a campaign not started, not an error);
        CORRUPTION is a loud ValueError naming where — silently skipping a
        torn row would undercount a population."""
        ...
