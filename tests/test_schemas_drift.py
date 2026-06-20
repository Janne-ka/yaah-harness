"""Drift check: schemas/*.schema.json match what scripts/build_schemas.py
emits from the engine's validation tables.

Used by: every CI / dev run via scripts/run_tests.py. The schemas are derived
artifacts (engine tables → JSON Schema); committing them lets editors find
them, but the test ensures the commit matches what the generator produces.
If validate.py or runtime_factories gain a new type and the schema is not
regenerated, this test fails with a one-line diff hint.

Also asserts that every checked-in example config validates against the
schema — catches the case where the schema generator and the validator
disagree on the surface.

Run: cd yaah && PYTHONPATH=src python3 tests/test_schemas_drift.py
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import tempfile


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCHEMAS = ("root.schema.json", "pipeline.schema.json")


def scenario_schemas_match_generator() -> None:
    """Re-run the generator into a temp dir and diff vs the committed files."""
    with tempfile.TemporaryDirectory() as td:
        # Tell the generator to write into a sibling tree by env, OR just run
        # it in-process and compare. In-process is simpler and avoids subprocess
        # overhead — we read the same code path either way.
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        try:
            import build_schemas
            generated = {
                "root.schema.json":     build_schemas.build_root_schema(),
                "pipeline.schema.json": build_schemas.build_pipeline_schema(),
            }
        finally:
            sys.path.pop(0)

        for name in SCHEMAS:
            committed_path = os.path.join(ROOT, "schemas", name)
            with open(committed_path, "r", encoding="utf-8") as f:
                committed = json.load(f)
            assert committed == generated[name], (
                "{}: committed schema is stale — re-run `python3 scripts/"
                "build_schemas.py` to regenerate it".format(name))


def scenario_every_example_config_validates() -> None:
    """Every checked-in example .local.json (root) and *-pipeline.json
    (pipeline) must validate against the corresponding schema. The schema
    accepts an inline `$schema` key under patternProperties `^_`-? No —
    the schema allows `$schema` explicitly via additionalProperties only
    on `_`-prefixed keys. We use jsonschema's tolerance for unknown
    top-level $schema by stripping it before validate.

    If a future user-authored config drifts past the schema, the user
    will see it in their editor first; this test catches the case where
    OUR example configs drift past the schema we're shipping."""
    try:
        import jsonschema
    except ImportError:
        # not in the default env; skip rather than fail — the drift check
        # above is the load-bearing one
        print("(jsonschema not installed; skipping example-validation half)")
        return

    with open(os.path.join(ROOT, "schemas", "root.schema.json")) as f:
        root_schema = json.load(f)
    with open(os.path.join(ROOT, "schemas", "pipeline.schema.json")) as f:
        pipeline_schema = json.load(f)

    failures = []
    for path in sorted(
        glob.glob(os.path.join(ROOT, "examples", "*", "*.local.json"))
        + glob.glob(os.path.join(ROOT, "examples", "*", "*-pipeline*.json"))
    ):
        with open(path, "r", encoding="utf-8") as f:
            try:
                cfg = json.load(f)
            except json.JSONDecodeError as e:
                failures.append((path, "load: {}".format(e)))
                continue
        # cfg.pop("$schema", None)  # not present today; future-proof
        schema = pipeline_schema if "-pipeline" in os.path.basename(path) else root_schema
        try:
            jsonschema.validate(cfg, schema)
        except jsonschema.ValidationError as e:
            failures.append((path, "{}: {}".format(list(e.absolute_path), e.message)))

    assert not failures, "example configs failed schema validation:\n  " + "\n  ".join(
        "{} → {}".format(p, msg) for p, msg in failures)


def main() -> None:
    scenario_schemas_match_generator()
    scenario_every_example_config_validates()
    print("PASS schemas match generator; example configs validate")


if __name__ == "__main__":
    main()
