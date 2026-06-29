"""JsonSchemaValidator — typed-I/O gate richer than JsonObjectValidator.

Used by: the `json_schema` node type (`build/builders.py:_build_json_schema`).
Where: in a stage's `validators:` list when the agent must return a
specifically-shaped object (nested types, enums, required keys), not just
any JSON dict.
Why: agents drift away from prompt-stub contracts under pressure
(`{"findings": "oops"}` where `findings` must be an array of objects). This
catches the drift BEFORE the bad output flows downstream.

Dependency-free by design — supports a JSON-Schema SUBSET (type, enum,
required, properties, items) via the shared `yaah.jsonschema.check_schema`
(the SAME checker an agent uses to self-validate its `output_schema`, so the
validator-node path and the agent-contract path can never diverge). Full JSON
Schema (allOf, patterns, $ref, ...) would be an optional `jsonschema`-backed
adapter later, if a real need appears.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
from typing import Any, Dict

from ..core import Envelope, Failure, NodeConfig, Verdict
from ..jsonio import extract_json
from ..jsonschema import check_schema


class JsonSchemaValidator:
    """Passes if payload[key] parses as JSON matching a JSON-Schema SUBSET."""

    def __init__(self, schema: Dict[str, Any], *, key: str = "raw") -> None:
        self._schema = schema
        self._key = key

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        raw = input.payload.get(self._key, "")
        try:
            # Y4/Y5: feed the declared `required` keys AND the full schema as a
            # weak-executor backstop — recovers an unquoted-key object (Y4) and, failing
            # that, plucks bare/enum/type:string values and the one-pair-per-line shape
            # (Y5), gated by the schema so nothing is fabricated. No-op if both absent.
            obj = extract_json(raw, keys=self._schema.get("required"), schema=self._schema)
        except json.JSONDecodeError as e:
            return Verdict.failed(Failure(
                "not_json", "output is not valid JSON: {}".format(e),
                "return a single JSON value")).to_envelope(input)
        errors = check_schema(obj, self._schema, "$")
        if errors:
            return Verdict.failed(Failure(
                "schema_mismatch", "; ".join(errors[:8]),
                "match the declared schema")).to_envelope(input)
        return Verdict.passed().to_envelope(input)
