"""JsonSchemaValidator — typed-I/O gate richer than JsonObjectValidator.

Used by: the `json_schema` node type (`build/builders.py:_build_json_schema`).
Where: in a stage's `validators:` list when the agent must return a
specifically-shaped object (nested types, enums, required keys), not just
any JSON dict.
Why: agents drift away from prompt-stub contracts under pressure
(`{"findings": "oops"}` where `findings` must be an array of objects). This
catches the drift BEFORE the bad output flows downstream.

Dependency-free by design — supports a JSON-Schema SUBSET (type, enum,
required, properties, items). Full JSON Schema (allOf, patterns, $ref, ...)
would be an optional `jsonschema`-backed adapter later, if a real need
appears.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from ..core import Envelope, Failure, NodeConfig, Verdict
from ..jsonio import extract_json

# JSON-Schema `type` name -> a predicate. A dependency-free subset (we borrow
# the IDEA of schema-validated stage I/O, not a whole type-system library).
# `integer`/`number`/`boolean` are disambiguated because in Python bool is a
# subclass of int.
_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _check_schema(value: Any, schema: Dict[str, Any], path: str) -> List[str]:
    """Recursively check `value` against a JSON-Schema SUBSET, returning a list
    of path-qualified error strings (empty = valid). Supported keywords: type,
    enum, required, properties, items. Pure so it's trivially testable and
    easy to extend (add a keyword = a branch here)."""
    errors: List[str] = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append("{}: {!r} not in enum {}".format(path, value, schema["enum"]))
    t = schema.get("type")
    if t is not None:
        check = _TYPE_CHECKS.get(t)
        if check is None:
            errors.append("{}: unknown schema type {!r}".format(path, t))
            return errors
        if not check(value):
            errors.append("{}: expected {}, got {}".format(path, t, type(value).__name__))
            return errors  # type wrong -> deeper checks would be noise
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append("{}: missing required key {!r}".format(path, key))
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                errors.extend(_check_schema(value[key], sub, "{}.{}".format(path, key)))
    if isinstance(value, list) and "items" in schema:
        for i, el in enumerate(value):
            errors.extend(_check_schema(el, schema["items"], "{}[{}]".format(path, i)))
    return errors


class JsonSchemaValidator:
    """Passes if payload[key] parses as JSON matching a JSON-Schema SUBSET."""

    def __init__(self, schema: Dict[str, Any], *, key: str = "raw") -> None:
        self._schema = schema
        self._key = key

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        raw = input.payload.get(self._key, "")
        try:
            obj = extract_json(raw)
        except json.JSONDecodeError as e:
            return Verdict.failed(Failure(
                "not_json", "output is not valid JSON: {}".format(e),
                "return a single JSON value")).to_envelope(input)
        errors = _check_schema(obj, self._schema, "$")
        if errors:
            return Verdict.failed(Failure(
                "schema_mismatch", "; ".join(errors[:8]),
                "match the declared schema")).to_envelope(input)
        return Verdict.passed().to_envelope(input)
