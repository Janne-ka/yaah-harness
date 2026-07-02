"""Node contract — agent SELF-VALIDATION against its declared `output_schema`.

`output_schema` started (Y4) as a parse-failure RECOVERY hint (its `required`
keys). This is the next contract increment: on the HAPPY path too (the model
emitted valid JSON), the agent checks the parsed object against the full schema
SUBSET (type/enum/required/properties/items) and fails loud on a mismatch —
so a stage that declares its output contract enforces it WITHOUT needing a
separate `json_schema` validator node. Opt-in: no output_schema -> no check
(byte-identical to before). Same checker the json_schema validator uses (one
implementation, in yaah.jsonschema), so the two paths can't diverge.

These are written to BREAK it: valid-but-off-contract output (the case that
used to slip through silently and die downstream), and the recovery/validation
gap (recovery only ever checked key PRESENCE — an enum-invalid recovered value
must still be caught here).

Run: cd yaah && PYTHONPATH=src python3 tests/test_agent_contract.py
"""
from __future__ import annotations

import asyncio

from yaah.core import Envelope, Kind, NodeConfig, Verdict
from yaah.agents import Agent, FakeProvider

CFG = NodeConfig(model="fake:1")


async def _invoke(resp, schema, *, parse=True):
    a = Agent(FakeProvider(responses=[resp]), "judge {{x}}", parse=parse,
              output_schema=schema, stage="judge")
    return await a.invoke(Envelope("task", {"x": "y"}), CFG)


def _failed(out):
    return out.kind == Kind.VERDICT and not Verdict.from_envelope(out).ok


def _code(out):
    return Verdict.from_envelope(out).failures[0].code


# --- the core new behavior: valid JSON, off contract, now fails loud ----------

async def valid_json_missing_required_now_fails() -> None:
    # BEFORE: valid JSON missing a required key passed silently (required was
    # only enforced on the recovery path) and died downstream. NOW: caught here.
    out = await _invoke('{"reason": "looks ok"}', {"required": ["verdict"]})
    assert _failed(out), out
    assert _code(out) == "schema_mismatch", Verdict.from_envelope(out).failures


async def wrong_type_fails() -> None:
    schema = {"type": "object", "properties": {"verdict": {"type": "string"}}}
    out = await _invoke('{"verdict": 123}', schema)
    assert _failed(out) and _code(out) == "schema_mismatch", out


async def enum_violation_fails() -> None:
    schema = {"properties": {"verdict": {"enum": ["FIX", "PASS"]}}}
    out = await _invoke('{"verdict": "MAYBE"}', schema)
    assert _failed(out) and _code(out) == "schema_mismatch", out


async def nested_array_item_type_fails() -> None:
    # findings must be an array of objects each with a string `id`
    schema = {"properties": {"findings": {"type": "array",
              "items": {"type": "object", "properties": {"id": {"type": "string"}}}}}}
    out = await _invoke('{"findings": [{"id": 7}]}', schema)
    assert _failed(out) and _code(out) == "schema_mismatch", out


# --- conforming output is untouched ------------------------------------------

async def matching_passes_and_merges() -> None:
    schema = {"type": "object", "required": ["verdict"],
              "properties": {"verdict": {"enum": ["FIX", "PASS"]}}}
    out = await _invoke('{"verdict": "FIX"}', schema)
    assert out.kind != Kind.VERDICT, out
    assert out.payload["verdict"] == "FIX", out.payload


async def no_schema_no_validation() -> None:
    # no output_schema -> any JSON dict passes (byte-identical to before)
    out = await _invoke('{"reason": "anything goes"}', None)
    assert out.kind != Kind.VERDICT, out
    assert out.payload["reason"] == "anything goes", out.payload


async def parse_false_no_validation() -> None:
    # parse:false means no extract_json at all -> no contract check; raw only
    out = await _invoke('{"reason": "x"}', {"required": ["verdict"]}, parse=False)
    assert out.payload["raw"] == '{"reason": "x"}', out.payload
    assert "reason" not in out.payload, out.payload


# --- the recovery/validation gap: recovery checks PRESENCE, contract the rest -

async def recovered_object_still_enum_checked() -> None:
    # unquoted-key output recovers (required key present) BUT the value violates
    # the enum -> recovery's presence-only check used to let it through; the
    # contract self-check must catch it.
    schema = {"required": ["verdict"],
              "properties": {"verdict": {"enum": ["FIX", "PASS"]}}}
    out = await _invoke('{verdict:"MAYBE"}', schema)
    assert _failed(out) and _code(out) == "schema_mismatch", out


async def recovered_object_valid_passes() -> None:
    schema = {"required": ["verdict"],
              "properties": {"verdict": {"enum": ["FIX", "PASS"]}}}
    out = await _invoke('{verdict:"FIX"}', schema)
    assert out.payload.get("verdict") == "FIX", out.payload


async def main() -> None:
    await valid_json_missing_required_now_fails()
    await wrong_type_fails()
    await enum_violation_fails()
    await nested_array_item_type_fails()
    await matching_passes_and_merges()
    await no_schema_no_validation()
    await parse_false_no_validation()
    await recovered_object_still_enum_checked()
    await recovered_object_valid_passes()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
