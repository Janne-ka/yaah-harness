"""Optional standard-library validators (generic, reusable). Not the kernel.

Domain validators live in the application; these are the few that are generic
enough to ship with YAAH (e.g. the JSON gate every structured-output agent needs).

This package replaces the former single-file `src/yaah/validators.py` — the
one-class-per-file rule that the project enforces on contributors now holds
for the engine's own validators too. Old `from yaah.validators import X`
imports still work via the re-exports below.

Targets Python 3.9+.
"""
from .expect_field_validator import ExpectField
from .json_object_validator import JsonObjectValidator
from .json_schema_validator import JsonSchemaValidator, _check_schema

# `_check_schema` is re-exported because tests + harness/decision_forms.py
# both reach for it as the dependency-free schema-subset checker. It is the
# private helper of JsonSchemaValidator; keep it accessible at the package
# top level so the import doesn't have to know about the file split.
__all__ = ["ExpectField", "JsonObjectValidator", "JsonSchemaValidator"]
