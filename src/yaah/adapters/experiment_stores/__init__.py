"""Experiment-row substrates — adapters for yaah.experiment.ExperimentStore.

JSONL (default: inspectable, deterministic, zero-dep) today; Postgres
(INSERT-only rows table, optional psycopg dep) is the planned production
adapter. Same port, swappable per campaign.
"""
from .jsonl_experiment_store import JsonlExperimentStore

__all__ = ["JsonlExperimentStore"]
