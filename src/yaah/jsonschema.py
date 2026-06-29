"""check_schema — the dependency-free JSON-Schema SUBSET checker.

Used by: the `json_schema` validator node (`validators/json_schema_validator.py`)
AND an agent's own output-contract check (`agents/agent.py`, when a stage
declares `output_schema`).
Where: any place a parsed object must match a declared shape before it flows on.
Why: agents drift away from their prompt-stub contracts under pressure
(`{"findings": "oops"}` where `findings` must be an array of objects). Catching
the drift at the seam beats a confusing failure downstream. One implementation
so the validator path and the agent self-check path can never diverge.

Dependency-free by design — a SUBSET (type, enum, required, properties, items).
Full JSON Schema (allOf, patterns, $ref, ...) would be an optional
`jsonschema`-backed adapter later, if a real need appears.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, List

# JSON-Schema `type` name -> a predicate. A dependency-free subset (we borrow
# the IDEA of schema-validated I/O, not a whole type-system library).
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


def check_schema(value: Any, schema: Dict[str, Any], path: str = "$") -> List[str]:
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
                errors.extend(check_schema(value[key], sub, "{}.{}".format(path, key)))
    if isinstance(value, list) and "items" in schema:
        for i, el in enumerate(value):
            errors.extend(check_schema(el, schema["items"], "{}[{}]".format(path, i)))
    return errors
