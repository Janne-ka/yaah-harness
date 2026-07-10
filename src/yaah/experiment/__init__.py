"""yaah.experiment — A/B experimentation over pipeline configs (workstream A).

The shape: an experiment is CONFIG (variants are `_extends` overlay pairs of
the production config), runs collect durable ROWS (one per run — done,
suspended, and failed alike), and the comparison report reads rows + traces.
Promotion is an edit: the winning variant's overlay merges into production,
because the experiment artifact and the production artifact are the same
species. One port (ExperimentStore) keeps the row substrate swappable.
"""
from .fingerprint import config_fingerprint
from .golden import golden_diff_rows, load_golden
from .report import build_matrix
from .rescore import rescore_rows
from .runner import run_experiment
from .experiment_store import ExperimentStore

__all__ = ["ExperimentStore", "build_matrix", "config_fingerprint",
           "golden_diff_rows", "load_golden", "rescore_rows", "run_experiment"]
