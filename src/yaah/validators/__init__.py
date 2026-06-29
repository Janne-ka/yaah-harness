"""Optional standard-library validators (generic, reusable). Not the kernel.

Domain validators live in the application; these are the few that are generic
enough to ship with YAAH (e.g. the JSON gate every structured-output agent needs).

This package replaces the former single-file `src/yaah/validators.py` — the
one-class-per-file rule that the project enforces on contributors now holds
for the engine's own validators too. Old `from yaah.validators import X`
imports still work via the re-exports below.

Targets Python 3.9+.
"""
from ..jsonschema import check_schema
from .expect_field_validator import ExpectField
from .json_object_validator import JsonObjectValidator
from .json_schema_validator import JsonSchemaValidator

# `check_schema` (the dependency-free schema-subset checker) now lives in
# `yaah.jsonschema` — the SAME checker an agent uses to self-validate its
# `output_schema`, so the validator-node and agent-contract paths can't
# diverge. Re-exported here for the callers (tests, harness/decision_forms)
# that reach for it via the validators package.
__all__ = ["ExpectField", "JsonObjectValidator", "JsonSchemaValidator", "check_schema"]
