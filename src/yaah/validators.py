"""Optional standard-library validators (generic, reusable). Not the kernel.

Domain validators live in the application; these are the few that are generic
enough to ship with YAAH (e.g. the JSON gate every structured-output agent needs).

Targets Python 3.9+.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .core import Envelope, Failure, NodeConfig, Verdict
from .jsonio import extract_json

# JSON-Schema `type` name -> a predicate. A dependency-free subset (we borrow the
# IDEA of schema-validated stage I/O, not a whole type-system library). `integer`/
# `number`/`boolean` are disambiguated because in Python bool is a subclass of int.
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
    """Recursively check `value` against a JSON-Schema SUBSET, returning a list of
    path-qualified error strings (empty = valid). Supported keywords: type,
    enum, required, properties, items. Used by JsonSchemaValidator; pure so it's
    trivially testable and easy to extend (add a keyword = a branch here)."""
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


class JsonObjectValidator:
    """Passes if payload[key] parses as a JSON object with the required keys."""

    def __init__(self, required: Optional[List[str]] = None, *, key: str = "raw") -> None:
        self._required = list(required or [])
        self._key = key

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        raw = input.payload.get(self._key, "")
        try:
            obj = extract_json(raw)
        except json.JSONDecodeError as e:
            return Verdict.failed(Failure(
                "not_json", "output is not valid JSON: {}".format(e),
                "return a single JSON object")).to_envelope(input)
        if not isinstance(obj, dict):
            return Verdict.failed(Failure(
                "not_object", "top level is not a JSON object",
                "return a JSON object")).to_envelope(input)
        missing = [k for k in self._required if k not in obj]
        if missing:
            return Verdict.failed(Failure(
                "missing_keys", "missing keys: {}".format(missing),
                "include keys {}".format(self._required))).to_envelope(input)
        return Verdict.passed().to_envelope(input)


class JsonSchemaValidator:
    """Passes if payload[key] parses as JSON matching a JSON-Schema SUBSET.

    The typed-I/O gate: richer than JsonObjectValidator (required keys only), it
    checks field TYPES and nested shape — so an agent that returns
    `{"findings": "oops"}` where `findings` must be an array of objects is caught
    BEFORE the bad output flows downstream (fights prompt-stub contract drift).
    Dependency-free by design: supports type / enum / required / properties /
    items (see _check_schema). Full JSON Schema (allOf, patterns, $ref, ...) would
    be an optional `jsonschema`-backed adapter later, if a real need appears.
    """

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


class ExpectField:
    """Passes if the prior node's output payload[key] equals an expected value.

    A structured counterpart to ShellCheck: instead of running a command, it
    asserts a field the previous node already produced. The RED gate uses it to
    require that the test run reported failure (ok == False) without re-running
    the suite.
    """

    def __init__(self, key: str, equals: object) -> None:
        self._key = key
        self._equals = equals

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        actual = input.payload.get(self._key)
        if actual == self._equals:
            return Verdict.passed().to_envelope(input)
        return Verdict.failed(Failure(
            "field_mismatch",
            "{} is {!r}, expected {!r}".format(self._key, actual, self._equals),
            "produce {}={!r}".format(self._key, self._equals),
        )).to_envelope(input)
