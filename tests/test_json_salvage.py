"""M44 multi-object salvage — the unreadable-veto fix on the agent's schema-parse path.

A schema-declaring agent that answers with its JSON answer PLUS a format example as a
second top-level object used to fail `not_json` ("ambiguous JSON: 2 top-level objects")
— rejecting a perfectly readable reply and burning the attempt (in the field: a TRUE
veto from a confirm agent was lost to exactly this). The fix: when the FULL parse
fails and an output_schema is declared, salvage the FIRST balanced top-level object
(json.JSONDecoder().raw_decode from the first `{`) and accept it IFF it validates
against the schema — with a `json_salvaged` note on the progress stream AND a
`json_salvage` trace span, so the salvage is never silent.

Deliberately NOT covered because it is deliberately NOT done: second-object shopping.
If the FIRST object fails the schema, the reply fails not_json exactly as before even
when a LATER object would validate — picking whichever object happens to validate
would let a trailing format example win over a malformed real answer. First-or-nothing
keeps the rule deterministic and keeps a bad first answer failing loud.

Run: PYTHONPATH=src python3 tests/test_json_salvage.py
"""
from __future__ import annotations

import asyncio

from yaah.core import Envelope, Kind, NodeConfig, Verdict
from yaah.agents import Agent, FakeProvider
from yaah.trace import RecordingTracer
from yaah.trace.contributors import PhaseContributor

CFG = NodeConfig(model="fake:1")

# The declared output contract: `enum` makes schema-validity DISCRIMINATING (an
# off-enum first object must fail salvage even though it has the right key).
SCHEMA = {"type": "object", "required": ["verdict"],
          "properties": {"verdict": {"enum": ["PASS", "FAIL"]}}}

ANSWER_PLUS_EXAMPLE = (
    '{"verdict":"PASS"}\n\n'
    'For reference, the expected format is:\n'
    '{"verdict":"<PASS|FAIL>"}'
)


async def _invoke(resp, schema, tracer=None):
    agent = Agent(FakeProvider(responses=[resp]), "judge {{x}}", parse=True,
                  output_schema=schema, stage="judge", tracer=tracer)
    return await agent.invoke(Envelope("task", {"x": "y"}), CFG)


def _failed(out, code):
    if out.kind != Kind.VERDICT:
        return False
    v = Verdict.from_envelope(out)
    return (not v.ok) and any(f.code == code for f in v.failures)


def _salvage_spans(tracer):
    return [s for s in tracer.spans if s.name == "json_salvage"]


async def answer_plus_example_is_salvaged_with_trace_note() -> None:
    # The M44 shape: the real answer FIRST, a format example SECOND. The full
    # parse fails ambiguous (both objects hold the required key); the salvage
    # accepts the schema-valid first object and the reply survives.
    tracer = RecordingTracer([PhaseContributor()])
    out = await _invoke(ANSWER_PLUS_EXAMPLE, SCHEMA, tracer)
    assert out.kind != Kind.VERDICT, out
    assert out.payload["verdict"] == "PASS", out.payload
    # ...and NEVER silently: the trace carries the json_salvage span + note.
    spans = _salvage_spans(tracer)
    assert len(spans) == 1, tracer.spans
    note = spans[0].attrs["note"]
    assert note == ("json_salvaged: first of 2 top-level values used; "
                    "trailing content discarded"), note
    assert spans[0].attrs["stage"] == "judge", spans[0].attrs
    # the note must reach the PROJECTED record (what a sink persists), not
    # sit dead in span.attrs — the phase capture projects it bounded.
    rec = [r for r in tracer.records if r.get("name") == "json_salvage"][0]
    assert rec.get("note") == note, rec


async def first_object_off_schema_fails_unchanged_no_shopping() -> None:
    # The FIRST object has the required key but an off-enum value; the SECOND
    # would validate. The salvage must NOT shop for the later object — the
    # reply fails not_json exactly as before the feature (deterministic,
    # no cherry-picking; see the module docstring for why).
    tracer = RecordingTracer([PhaseContributor()])
    out = await _invoke('{"verdict":"MAYBE"}\n{"verdict":"PASS"}', SCHEMA, tracer)
    assert _failed(out, "not_json"), out
    f = Verdict.from_envelope(out).failures[0]
    assert "the output began" in f.message, f.message   # v0.3.2 reply sample intact
    assert not _salvage_spans(tracer), tracer.spans     # and no salvage note


async def prose_only_reply_fails_unchanged() -> None:
    # No balanced object at all -> the existing not_json failure, byte-identical
    # (the reply sample still names the prose refusal).
    tracer = RecordingTracer([PhaseContributor()])
    out = await _invoke("I cannot produce a verdict for this input.", SCHEMA, tracer)
    assert _failed(out, "not_json"), out
    f = Verdict.from_envelope(out).failures[0]
    assert "the output began" in f.message, f.message
    assert not _salvage_spans(tracer), tracer.spans


async def no_schema_declared_no_salvage() -> None:
    # WITHOUT an output_schema there is no gate that makes accepting a fragment
    # safe, so the salvage must not run. The multi-object reply takes the legacy
    # no-keys path (first balanced span) — same value as before, and NO salvage
    # span/note anywhere.
    tracer = RecordingTracer([PhaseContributor()])
    out = await _invoke(ANSWER_PLUS_EXAMPLE, None, tracer)
    assert out.kind != Kind.VERDICT, out
    assert out.payload["verdict"] == "PASS", out.payload    # legacy first-span pick
    assert not _salvage_spans(tracer), tracer.spans

    # ...and a reply the legacy path REJECTS stays rejected without salvage.
    tracer2 = RecordingTracer([PhaseContributor()])
    out2 = await _invoke('not json at all', None, tracer2)
    assert _failed(out2, "not_json"), out2
    assert not _salvage_spans(tracer2), tracer2.spans


async def single_valid_object_identical_no_note() -> None:
    # The normal path is byte-identical: one valid object parses via
    # extract_json, the salvage never runs, no note is emitted.
    tracer = RecordingTracer([PhaseContributor()])
    out = await _invoke('{"verdict":"PASS"}', SCHEMA, tracer)
    assert out.kind != Kind.VERDICT, out
    assert out.payload["verdict"] == "PASS", out.payload
    assert not _salvage_spans(tracer), tracer.spans
    assert not [r for r in tracer.records if "note" in r], tracer.records


async def leading_array_not_object_arm_salvages() -> None:
    # The not_object arm: with no `required` (so no keys-guided candidate scan),
    # the legacy parse returns the FIRST balanced span — a leading array — and
    # would fail not_object; the salvage recovers the schema-valid object that
    # sits beside it.
    schema = {"type": "object",
              "properties": {"verdict": {"enum": ["PASS", "FAIL"]}}}
    tracer = RecordingTracer([PhaseContributor()])
    out = await _invoke('[1, 2]\n{"verdict":"FAIL"}', schema, tracer)
    assert out.kind != Kind.VERDICT, out
    assert out.payload["verdict"] == "FAIL", out.payload
    spans = _salvage_spans(tracer)
    assert len(spans) == 1 and spans[0].attrs["note"].startswith("json_salvaged:"), \
        tracer.spans


async def main() -> None:
    await answer_plus_example_is_salvaged_with_trace_note()
    await first_object_off_schema_fails_unchanged_no_shopping()
    await prose_only_reply_fails_unchanged()
    await no_schema_declared_no_salvage()
    await single_valid_object_identical_no_note()
    await leading_array_not_object_arm_salvages()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
