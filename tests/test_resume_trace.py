"""Resume trace record: harness.resume() must LOG the human decision.

Before this feature the trace of a gated run ended at status:suspended — the
resume (the human override, the AI-Act Art. 14(4)(d) "override is logged"
record) left no span at all, and the baton was deleted on completion, so the
decision event was unreconstructable. resume() now emits a point-in-time
`stage` record: status "ok", attrs {resumed: true, awaiting, decision_keys}.
Decision KEYS only — payload VALUES may be sensitive and must never reach the
trace.

These tests assert on the PROJECTED record (what a sink actually receives),
not raw span attrs — a whitelist miss in PhaseContributor would make the
attrs dead in real runs (the M7 ladder_from lesson).

Run: cd yaah && PYTHONPATH=src python3 tests/test_resume_trace.py
"""
from __future__ import annotations

import asyncio
import json

from yaah import (
    Done,
    Envelope,
    Failure,
    Graph,
    Harness,
    InProcessComms,
    NodeConfig,
    Stage,
    Suspended,
    Verdict,
)
from yaah.trace import RecordingTracer
from yaah.trace.aggregate import aggregate
from yaah.trace.contributors import PhaseContributor
from yaah.trace.pretty import pretty

# A sentinel VALUE the human's decision carries. Its KEY may appear in the
# trace (decision_keys); this VALUE must never appear anywhere in any record
# or span — that's the falsifier for the keys-only contract.
SECRET = "hunter2-SENTINEL-VALUE-must-not-be-traced"


# --- nodes (mirrors test_harness.py's gate cast) ------------------------------

class Stubborn:
    """Never satisfies the validator — forces the escalate-to-human park."""

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply("result", text="nope", ok=False)


class OkValidator:
    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        if input.payload.get("ok"):
            return Verdict.passed().to_envelope()
        return Verdict.failed(Failure("not_ok", "needs ok=true", "set ok=true")).to_envelope()


class GateNode:
    """A plain gate: parks the run itself (Kind.AWAIT), no failed artifact."""

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply("await", awaiting="human:plain", ask="approve?")


class Upper:
    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply("result", text=str(input.payload.get("text", "")).upper())


# --- helpers ------------------------------------------------------------------

def _resume_records(tr: RecordingTracer) -> list:
    return [r for r in tr.records if r.get("resumed") is True]


def _park(tr: RecordingTracer, comms: InProcessComms, graph: Graph) -> Harness:
    return Harness(comms, graph, tracer=tr)


# --- scenarios ----------------------------------------------------------------

async def scenario_resume_emits_projected_record() -> None:
    """The core falsifier: after park→resume the trace carries a resume record
    — on the PROJECTED record, with decision keys, the run's corr, status ok,
    the parked stage's name, and the baton's awaiting."""
    comms = InProcessComms()
    comms.register("role:stubborn", Stubborn())
    comms.register("role:check", OkValidator())
    graph = Graph.of(
        Stage("gate", node="role:stubborn", validators=["role:check"],
              max_attempts=2, feedback=True, escalate="human"))
    tr = RecordingTracer([PhaseContributor()])
    harness = _park(tr, comms, graph)

    outcome = await harness.run(Envelope("task", {}))
    assert isinstance(outcome, Suspended), outcome
    parked = [r for r in tr.records
              if r.get("name") == "stage" and r.get("status") == "suspended"]
    assert parked, "precondition: the park itself must be traced"
    run_corr = parked[-1]["corr"]
    n_before = len(tr.records)
    assert _resume_records(tr) == [], "no resume record before resume()"

    decision = Envelope("result", {"text": "human-approved", "ok": True,
                                   "secret_token": SECRET})
    final = await harness.resume(outcome.baton_id, decision)
    assert isinstance(final, Done), final

    recs = _resume_records(tr)
    assert len(recs) == 1, "exactly one resume record, got: {!r}".format(recs)
    rec = recs[0]
    # projected shape (what a sink sees) — every attr must survive PhaseContributor
    assert rec["name"] == "stage", rec
    assert rec["stage"] == "gate", rec
    assert rec["status"] == "ok", rec
    assert rec["awaiting"] == "human:gate", rec
    assert rec["decision_keys"] == ["ok", "secret_token", "text"], rec  # sorted, keys only
    assert rec["corr"] == run_corr, "resume record must join the run's trace: {!r}".format(rec)
    assert tr.records.index(rec) >= n_before, "emitted at resume time, not during the run"


async def scenario_decision_values_never_leak() -> None:
    """Falsifier for the keys-only contract: the sentinel VALUE must not appear
    in ANY projected record nor ANY raw span of the whole run — including the
    post-resume stages the merged decision flows through."""
    comms = InProcessComms()
    comms.register("role:stubborn", Stubborn())
    comms.register("role:check", OkValidator())
    comms.register("role:upper", Upper())
    graph = Graph.of(
        Stage("gate", node="role:stubborn", validators=["role:check"],
              max_attempts=1, escalate="human", then="after"),
        Stage("after", node="role:upper"))
    tr = RecordingTracer([PhaseContributor()])
    harness = _park(tr, comms, graph)

    outcome = await harness.run(Envelope("task", {}))
    assert isinstance(outcome, Suspended), outcome
    final = await harness.resume(
        outcome.baton_id,
        Envelope("result", {"text": "fine", "ok": True, "secret_token": SECRET}))
    assert isinstance(final, Done), final
    assert final.output.payload["text"] == "FINE", final.output  # decision DID flow downstream

    # the whole trace must be JSON-serializable AND free of the sentinel value
    dumped = json.dumps(tr.records)
    assert SECRET not in dumped, "decision VALUE leaked into a projected record"
    assert "secret_token" in dumped, "the KEY should be present (decision_keys)"
    for span in tr.spans:
        assert SECRET not in repr(span.attrs), \
            "decision VALUE leaked into raw span attrs: {!r}".format(span.attrs)


async def scenario_plain_gate_resume_record() -> None:
    """A gate NODE (Kind.AWAIT park, not a validator escalate) gets the same
    record; awaiting reflects the node's own awaiting string."""
    comms = InProcessComms()
    comms.register("role:gate", GateNode())
    graph = Graph.of(Stage("approve", node="role:gate"))
    tr = RecordingTracer([PhaseContributor()])
    harness = _park(tr, comms, graph)

    task = Envelope("task", {"text": "hello"})
    outcome = await harness.run(task)
    assert isinstance(outcome, Suspended), outcome
    final = await harness.resume(outcome.baton_id, Envelope("result", {"approved": True}))
    assert isinstance(final, Done), final

    [rec] = _resume_records(tr)
    assert rec["stage"] == "approve", rec
    assert rec["awaiting"] == "human:plain", rec
    assert rec["decision_keys"] == ["approved"], rec
    assert rec["corr"] == task.correlation_id, rec  # merged input keeps the run's corr


async def scenario_aggregate_and_pretty_stay_sane() -> None:
    """The resume record must not read as a failure: aggregate's error/retry
    counts are unchanged by it, and `yaah trace --pretty` renders the records
    without choking (accept criterion)."""
    comms = InProcessComms()
    comms.register("role:stubborn", Stubborn())
    comms.register("role:check", OkValidator())
    graph = Graph.of(
        Stage("gate", node="role:stubborn", validators=["role:check"],
              max_attempts=1, escalate="human"))
    tr = RecordingTracer([PhaseContributor()])
    harness = _park(tr, comms, graph)

    outcome = await harness.run(Envelope("task", {}))
    assert isinstance(outcome, Suspended), outcome
    before = aggregate(list(tr.records))

    final = await harness.resume(outcome.baton_id,
                                 Envelope("result", {"text": "ok", "ok": True}))
    assert isinstance(final, Done), final
    after = aggregate(list(tr.records))

    # the resume record contributes NO error and NO retry (status is plain "ok")
    assert after["totals"]["errors"] == before["totals"]["errors"], (before, after)
    assert after["totals"]["retries"] == before["totals"]["retries"], (before, after)

    out = pretty(tr.records)
    assert "gate" in out, out                       # renders as a normal stage line
    assert "error" not in out.splitlines()[0], out  # header reports no error for it


async def main() -> None:
    await scenario_resume_emits_projected_record()
    await scenario_decision_values_never_leak()
    await scenario_plain_gate_resume_record()
    await scenario_aggregate_and_pretty_stay_sane()
    print("PASS test_resume_trace")


if __name__ == "__main__":
    asyncio.run(main())
