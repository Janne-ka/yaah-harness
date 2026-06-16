"""JsonSchemaValidator: the dependency-free JSON-Schema-subset typed-I/O gate,
the recursive _check_schema, and config-driven use as a stage validator.

Run: cd yaah && PYTHONPATH=src python3 tests/test_schema_validator.py
"""
from __future__ import annotations

import asyncio
import json

from yaah import Done, Envelope, NodeConfig
from yaah.build import build
from yaah.agents import ScriptedBackend
from yaah.validators import JsonSchemaValidator, _check_schema

CFG = NodeConfig()

# a representative stage contract: an object with a typed array-of-objects field
SCHEMA = {
    "type": "object",
    "required": ["summary", "findings"],
    "properties": {
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "severity"],
                "properties": {
                    "id": {"type": "string"},
                    "severity": {"type": "string", "enum": ["low", "high"]},
                },
            },
        },
    },
}


def scenario_check_schema_pure() -> None:
    ok = {"summary": "s", "findings": [{"id": "F1", "severity": "high"}]}
    assert _check_schema(ok, SCHEMA, "$") == []

    # wrong type for a nested field
    bad_type = {"summary": "s", "findings": "oops"}
    errs = _check_schema(bad_type, SCHEMA, "$")
    assert any("findings" in e and "expected array" in e for e in errs), errs

    # missing required nested key + bad enum, path-qualified to the array index
    bad_item = {"summary": "s", "findings": [{"id": "F1", "severity": "nope"}]}
    errs = _check_schema(bad_item, SCHEMA, "$")
    assert any("findings[0].severity" in e and "enum" in e for e in errs), errs

    # missing top-level required key
    assert any("missing required key 'summary'" in e
               for e in _check_schema({"findings": []}, SCHEMA, "$"))

    # bool is NOT an integer (Python subclass trap)
    assert _check_schema(True, {"type": "integer"}, "$") != []
    assert _check_schema(3, {"type": "integer"}, "$") == []
    # ... but a plain number accepts int and float, not bool
    assert _check_schema(3.5, {"type": "number"}, "$") == []
    assert _check_schema(False, {"type": "number"}, "$") != []


async def scenario_validator_node() -> None:
    v = JsonSchemaValidator(SCHEMA)
    good = Envelope("result", {"raw": json.dumps({"summary": "s",
                                                  "findings": [{"id": "F1", "severity": "low"}]})})
    verdict = await v.invoke(good, CFG)
    assert verdict.payload["status"] == "pass", verdict.payload

    bad = Envelope("result", {"raw": json.dumps({"summary": "s", "findings": [{"id": "F1"}]})})
    verdict = await v.invoke(bad, CFG)
    assert verdict.payload["status"] == "fail"
    assert "severity" in json.dumps(verdict.payload)  # the missing key is named

    notjson = Envelope("result", {"raw": "not json at all"})
    verdict = await v.invoke(notjson, CFG)
    assert verdict.payload["status"] == "fail" and verdict.payload["failures"][0]["code"] == "not_json"


async def scenario_config_driven() -> None:
    # an agent stage gated by a json_schema validator: a good output passes;
    # a contract-violating output fails the stage (retry/escalate would kick in).
    config = {
        "nodes": {
            "writer": {"type": "agent", "template": "x", "model": "fake:writer"},
            "shape": {"type": "json_schema", "schema": SCHEMA},
        },
        "graph": {"start": "s", "stages": {"s": {"node": "writer", "validators": ["shape"]}}},
    }
    good = json.dumps({"summary": "ok", "findings": []})
    backend = ScriptedBackend({"fake:writer": [good]})
    h = build(config, backend=backend)
    out = await h.run(Envelope("task", {}))
    assert isinstance(out, Done) and out.output.payload["raw"] == good


async def main() -> None:
    scenario_check_schema_pure()
    await scenario_validator_node()
    await scenario_config_driven()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
