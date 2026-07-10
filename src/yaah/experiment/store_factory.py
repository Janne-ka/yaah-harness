"""store_from_config — single construction point for ExperimentStore (AB-T3).

Used by: the three `yaah ab` verbs — runner, report, rescore — wherever they
  need to build the row store from the experiment config's `store` block.
Where: yaah.experiment — sits beside the other experiment-layer modules.
Why: three identical if/else store-construction blocks existed across runner,
  report, and rescore; this factory is the single place where dispatch lives.

Dispatches on store["type"]:
  (absent) or "jsonl" → JsonlExperimentStore(dir)
  "postgres"          → PostgresExperimentStore(dsn=..., table=...)

Unknown `type` and unknown keys per type raise ValueError naming the known set,
mirroring runtime_factories._STATE_TYPES and validate.py's stance.

IMPORTANT — store.dir is always the campaign directory, regardless of store type.
The campaign trace file (runner.py trace_path) always lands in a directory; callers
compute store_dir from cfg even when the row store is postgres. The default is ".ab"
in both cases. This factory does NOT return store_dir — callers that need it
(runner, report) derive it from cfg themselves and use it for the trace file.

Targets Python 3.9+.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional

from .experiment_store import ExperimentStore

# Single source of truth for store-block dispatch — _check_experiment (runner.py)
# validates against THESE sets, so validation and construction cannot diverge.
STORE_TYPES = frozenset({"jsonl", "postgres"})
STORE_KEYS: Dict[str, frozenset] = {
    "jsonl":    frozenset({"type", "dir"}),
    "postgres": frozenset({"type", "dir", "dsn", "table"}),
}


def store_from_config(cfg: Dict[str, Any], base: str) -> ExperimentStore:
    """Build the ExperimentStore declared in cfg's `store` block.

    `base` is the directory from which relative `dir` values are resolved
    (the same semantics as `runtime_factories._rel`).
    """
    from ..runtime_factories import _rel
    from ..adapters.experiment_stores import JsonlExperimentStore
    from ..adapters.experiment_stores.postgres_experiment_store import PostgresExperimentStore

    store_cfg: Dict[str, Any] = cfg.get("store") or {}
    store_type: str = store_cfg.get("type", "jsonl")
    # store.dir is always the campaign directory — trace files land here even when
    # the row store is postgres. Default ".ab" applies for both types.
    store_dir: str = _rel(base, store_cfg.get("dir", ".ab"))

    if store_type not in STORE_TYPES:
        raise ValueError(
            "unknown store type {!r} — known: {}".format(
                store_type,
                ", ".join(repr(t) for t in sorted(STORE_TYPES))))

    known_keys = STORE_KEYS[store_type]
    unknown = [k for k in store_cfg if k not in known_keys]
    if unknown:
        raise ValueError(
            "unknown key(s) in store block for type {!r}: {} — "
            "known: {}".format(
                store_type,
                ", ".join(repr(k) for k in unknown),
                ", ".join(repr(k) for k in sorted(known_keys))))

    if store_type == "jsonl":
        return JsonlExperimentStore(store_dir)

    # store_type == "postgres"
    dsn = store_cfg.get("dsn")
    if not dsn:
        raise ValueError(
            'store type "postgres" requires "dsn" — '
            'add {"dsn": "postgresql://user:pass@host/db"} to your store block')
    kw: Dict[str, Any] = {}
    if "table" in store_cfg:
        kw["table"] = store_cfg["table"]
    return PostgresExperimentStore(dsn, **kw)


@asynccontextmanager
async def opened_store(cfg: Dict[str, Any], base: str,
                       store: Optional[ExperimentStore] = None
                       ) -> AsyncIterator[ExperimentStore]:
    """The store for one verb invocation, closed on exit iff WE built it.

    A caller-injected `store` is caller-owned — yielded untouched, never closed
    (tests and embedding apps manage its lifetime). When `store` is None we
    build from cfg and close on exit; close() is not on the ExperimentStore
    port (jsonl has nothing to close), so it's an adapter capability probed
    with getattr — without it the postgres adapter's connection would leak
    once per verb (adversarial-eval finding)."""
    if store is not None:
        yield store
        return
    built = store_from_config(cfg, base)
    try:
        yield built
    finally:
        close = getattr(built, "close", None)
        if close is not None:
            await close()
