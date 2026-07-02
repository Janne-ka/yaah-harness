"""Y4 on the parse:true path — an agent that declares `output_schema` recovers a weak
model's known malformed shapes (unquoted keys etc.) ON ITS OWN PARSE, gated + bounded
so a masking reply still fails loud. This is the path that actually killed the s_factory
run (judge-findings, parse:true) — so the break-it cases run through Agent.invoke, NOT
extract_json in isolation.

Run: cd yaah && PYTHONPATH=src python3 tests/test_agent_output_schema.py
"""
from __future__ import annotations

import asyncio

from yaah.core import Envelope, Kind, NodeConfig, Verdict
from yaah.agents import Agent, FakeProvider

CFG = NodeConfig(model="fake:1")


def _agent(resp, schema):
    return Agent(FakeProvider(responses=[resp]), "judge {{x}}", parse=True,
                 output_schema=schema, stage="judge")


async def _invoke(resp, schema):
    return await _agent(resp, schema).invoke(Envelope("task", {"x": "y"}), CFG)


def _failed(out):
    return out.kind == Kind.VERDICT and not Verdict.from_envelope(out).ok


async def recovers_unquoted_keys_on_parse_path() -> None:
    # the judge-findings shape: a parse:true agent emits unquoted-key JSON
    out = await _invoke('{verdict:"FIX"}', {"required": ["verdict"]})
    assert out.kind != Kind.VERDICT, out
    assert out.payload["verdict"] == "FIX", out.payload


async def no_schema_unchanged_still_not_json() -> None:
    # WITHOUT output_schema -> no recovery -> not_json (byte-identical to before)
    out = await _invoke('{verdict:"FIX"}', None)
    assert _failed(out), out


async def masking_reply_recovers_right_value() -> None:
    # a key named inside another value must NOT mask the real top-level key
    out = await _invoke('{note:"verdict: SKIP", verdict:"FIX"}', {"required": ["verdict"]})
    assert out.payload.get("verdict") == "FIX", out.payload  # not SKIP


async def masking_only_in_string_fails_loud() -> None:
    # verdict appears ONLY inside a string value -> must fail, never fabricate
    out = await _invoke('{note:"verdict: SKIP"}', {"required": ["verdict"]})
    assert _failed(out), out


async def empty_reply_fails_loud() -> None:
    out = await _invoke('', {"required": ["verdict"]})
    assert _failed(out), out


async def wellformed_json_unaffected() -> None:
    out = await _invoke('{"verdict":"FIX"}', {"required": ["verdict"]})
    assert out.payload["verdict"] == "FIX", out.payload


async def parse_false_never_recovers() -> None:
    # output_schema + parse:false: recovery must NOT run (parse:false = no extract_json
    # at all). raw passes through; nothing merged.
    a = Agent(FakeProvider(responses=['{verdict:"FIX"}']), "judge {{x}}", parse=False,
              output_schema={"required": ["verdict"]}, stage="judge")
    out = await a.invoke(Envelope("task", {"x": "y"}), CFG)
    assert out.payload["raw"] == '{verdict:"FIX"}', out.payload
    assert "verdict" not in out.payload, out.payload


async def all_or_nothing_no_half_merge() -> None:
    # a required key the model omitted -> recovery returns None -> not_json; the payload
    # is never half-populated with the keys that WERE found.
    out = await _invoke('{verdict:"FIX"}', {"required": ["verdict", "confidence"]})
    assert _failed(out), out


async def empty_required_no_recovery() -> None:
    # required:[] (or no required) collapses to no recovery -> unquoted still fails
    out = await _invoke('{verdict:"FIX"}', {"required": []})
    assert _failed(out), out


async def main() -> None:
    await recovers_unquoted_keys_on_parse_path()
    await no_schema_unchanged_still_not_json()
    await masking_reply_recovers_right_value()
    await masking_only_in_string_fails_loud()
    await empty_reply_fails_loud()
    await wellformed_json_unaffected()
    await parse_false_never_recovers()
    await all_or_nothing_no_half_merge()
    await empty_required_no_recovery()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
