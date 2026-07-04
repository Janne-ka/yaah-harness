"""Experiment-row substrates — adapters for yaah.experiment.ExperimentStore.

JSONL (default: inspectable, deterministic, zero-dep) and Postgres (INSERT-only
rows table, optional psycopg dep). Same port, swappable per campaign via the
store block in the experiment config.
"""
from .jsonl_experiment_store import JsonlExperimentStore
from .postgres_experiment_store import PostgresExperimentStore

__all__ = ["JsonlExperimentStore", "PostgresExperimentStore"]
