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


class ConfigurableGate:
    """A plain gate whose awaiting/ask are set per-instance — lets one graph carry
    two distinct gates so a two-resume run can be checked for distinct records."""

    def __init__(self, awaiting: str, ask: str = "approve?") -> None:
        self._awaiting = awaiting
        self._ask = ask

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply("await", awaiting=self._awaiting, ask=self._ask)


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


# --- approver identity (AI-Act Art. 14(4)(d): WHO overrode) -------------------

async def scenario_approver_identity_recorded() -> None:
    """An OPTIONAL approver identity, supplied as the reserved `approver` HEADER on
    the resume envelope, lands on the projected record as `approver` — identity
    only. It must NOT leak into the payload / decision_keys / downstream, and the
    decision VALUE (SECRET) still must not appear."""
    comms = InProcessComms()
    comms.register("role:gate", GateNode())
    graph = Graph.of(Stage("approve", node="role:gate"))
    tr = RecordingTracer([PhaseContributor()])
    harness = _park(tr, comms, graph)

    outcome = await harness.run(Envelope("task", {}))
    assert isinstance(outcome, Suspended), outcome
    decision = Envelope("result", {"approved": True, "secret_token": SECRET},
                        headers={"approver": "alice@example.com"})
    final = await harness.resume(outcome.baton_id, decision)
    assert isinstance(final, Done), final

    [rec] = _resume_records(tr)
    assert rec["approver"] == "alice@example.com", rec
    # identity is metadata, not a decision key — it must not masquerade as one
    assert "approver" not in rec["decision_keys"], rec
    dumped = json.dumps(tr.records)
    assert "alice@example.com" in dumped, "the identity IS the audit signal"
    assert SECRET not in dumped, "decision VALUE must still never be traced"


async def scenario_approver_absent_omitted() -> None:
    """Backward compatibility: a resume WITHOUT an approver header produces a
    record with NO `approver` attr at all (today's records are unchanged)."""
    comms = InProcessComms()
    comms.register("role:gate", GateNode())
    graph = Graph.of(Stage("approve", node="role:gate"))
    tr = RecordingTracer([PhaseContributor()])
    harness = _park(tr, comms, graph)

    outcome = await harness.run(Envelope("task", {}))
    final = await harness.resume(outcome.baton_id, Envelope("result", {"approved": True}))
    assert isinstance(final, Done), final
    [rec] = _resume_records(tr)
    assert "approver" not in rec, "no approver header ⇒ no approver attr, got: {!r}".format(rec)


# --- AB-5 gate-outcome capture (emitted vs edited, keys only) ------------------

async def scenario_gate_diff_escalate() -> None:
    """The resume record carries a key-level `decision_diff` of the gate's EMITTED
    artifact vs the human's edit: emitted keys, keys the human ADDED, keys the
    human CHANGED (value differs). Keys only — no values. On the escalate path the
    emitted artifact is the failed stage's last output (text/ok) plus `escalation`."""
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
    # Stubborn's failed artifact: text="nope", ok=False; escalate adds `escalation`.
    # Human overrides text+ok (changed) and adds a new note (added).
    decision = Envelope("result", {"text": "human-approved", "ok": True,
                                   "note": "override rationale " + SECRET})
    final = await harness.resume(outcome.baton_id, decision)
    assert isinstance(final, Done), final

    [rec] = _resume_records(tr)
    diff = rec["decision_diff"]
    assert diff["emitted"] == ["escalation", "ok", "text"], diff  # sorted emitted keys
    assert diff["added"] == ["note"], diff                        # new key the human introduced
    assert diff["changed"] == ["ok", "text"], diff                # overridden (value differs)
    assert "truncated" not in diff, diff                          # small payload, no cap
    assert SECRET not in json.dumps(tr.records), "diff must store keys, never values"


async def scenario_gate_diff_plain() -> None:
    """A plain gate: emitted = what flowed into the gate + the gate's ask/awaiting;
    the human's single new key is `added`, nothing `changed`."""
    comms = InProcessComms()
    comms.register("role:gate", GateNode())
    graph = Graph.of(Stage("approve", node="role:gate"))
    tr = RecordingTracer([PhaseContributor()])
    harness = _park(tr, comms, graph)

    outcome = await harness.run(Envelope("task", {"text": "hello"}))
    assert isinstance(outcome, Suspended), outcome
    final = await harness.resume(outcome.baton_id, Envelope("result", {"approved": True}))
    assert isinstance(final, Done), final

    [rec] = _resume_records(tr)
    diff = rec["decision_diff"]
    assert diff["emitted"] == ["ask", "awaiting", "text"], diff
    assert diff["added"] == ["approved"], diff
    assert diff["changed"] == [], diff


async def scenario_gate_diff_bounded() -> None:
    """A pathological decision with many keys must not blow up the trace: each
    diff list is capped and flagged `truncated`; no VALUE ever enters the record."""
    comms = InProcessComms()
    comms.register("role:gate", GateNode())
    graph = Graph.of(Stage("approve", node="role:gate"))
    tr = RecordingTracer([PhaseContributor()])
    harness = _park(tr, comms, graph)

    outcome = await harness.run(Envelope("task", {}))
    assert isinstance(outcome, Suspended), outcome
    big = {"k{:03d}".format(i): "{}-{}".format(SECRET, i) for i in range(200)}
    final = await harness.resume(outcome.baton_id, Envelope("result", big))
    assert isinstance(final, Done), final

    [rec] = _resume_records(tr)
    diff = rec["decision_diff"]
    assert len(diff["added"]) <= 40, "added list must be capped, got {}".format(len(diff["added"]))
    assert diff["truncated"] is True, diff
    assert SECRET not in json.dumps(tr.records), "no decision VALUE may enter the record"


async def scenario_approver_header_never_flows_downstream() -> None:
    """Eval finding: a fanout suspend parks with NO artifact (pending=None), and
    _merge_decision used to return the response Envelope VERBATIM — carrying the
    reserved `approver` header into the merged input and downstream. The identity
    must be recorded on the span and then STOP there."""
    comms = InProcessComms()
    comms.register("role:gate", GateNode())

    class HeaderProbe:
        async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
            return input.reply("result", saw_approver="approver" in input.headers)

    comms.register("role:probe", HeaderProbe())
    graph = Graph.of(
        # fanout member AWAIT ⇒ _Suspend with no artifact ⇒ pending=None
        # (node= is unused on a fanout stage but required by the dataclass)
        Stage("par", node="role:gate", fanout=["role:gate"], then="after"),
        Stage("after", node="role:probe"))
    tr = RecordingTracer([PhaseContributor()])
    harness = _park(tr, comms, graph)

    outcome = await harness.run(Envelope("task", {}))
    assert isinstance(outcome, Suspended), outcome
    final = await harness.resume(
        outcome.baton_id,
        Envelope("result", {"approved": True}, headers={"approver": "alice@example.com"}))
    assert isinstance(final, Done), final

    assert final.output.payload["saw_approver"] is False, \
        "reserved approver header leaked into the downstream flow"
    [rec] = _resume_records(tr)
    assert rec["approver"] == "alice@example.com", rec   # recorded on the span...
    assert rec["decision_diff"]["emitted"] == [], rec    # ...and the no-artifact diff is sane


async def scenario_gate_diff_bounds_key_length() -> None:
    """Eval finding: the 40-key cap bounds key COUNT, not key SIZE — one megabyte
    key NAME would bloat the record. Each stored key must be length-clipped and
    the clip flagged `truncated`."""
    comms = InProcessComms()
    comms.register("role:gate", GateNode())
    graph = Graph.of(Stage("approve", node="role:gate"))
    tr = RecordingTracer([PhaseContributor()])
    harness = _park(tr, comms, graph)

    outcome = await harness.run(Envelope("task", {}))
    assert isinstance(outcome, Suspended), outcome
    huge_key = "k" * 10_000
    final = await harness.resume(outcome.baton_id, Envelope("result", {huge_key: True}))
    assert isinstance(final, Done), final

    [rec] = _resume_records(tr)
    diff = rec["decision_diff"]
    assert all(len(k) <= 200 for k in diff["added"]), \
        "stored key names must be length-bounded, got len={}".format(
            max(len(k) for k in diff["added"]))
    assert diff["truncated"] is True, diff


async def scenario_two_resumes_distinct_diffs() -> None:
    """Two sequential gates ⇒ two resumes ⇒ two DISTINCT resume records that
    ACCUMULATE (no double-recording, no overwrite — the audit wants a durable
    history): each carries its own stage, decision_diff, and approver (two
    different humans must both stay on record)."""
    comms = InProcessComms()
    comms.register("role:g1", ConfigurableGate("human:one"))
    comms.register("role:g2", ConfigurableGate("human:two"))
    graph = Graph.of(
        Stage("first", node="role:g1", then="second"),
        Stage("second", node="role:g2"))
    tr = RecordingTracer([PhaseContributor()])
    harness = _park(tr, comms, graph)

    out1 = await harness.run(Envelope("task", {}))
    assert isinstance(out1, Suspended), out1
    out2 = await harness.resume(
        out1.baton_id, Envelope("result", {"a": 1}, headers={"approver": "alice"}))
    assert isinstance(out2, Suspended), out2       # parks again at the second gate
    final = await harness.resume(
        out2.baton_id, Envelope("result", {"b": 2}, headers={"approver": "bob"}))
    assert isinstance(final, Done), final

    recs = _resume_records(tr)
    assert len(recs) == 2, "one record per resume, got {}".format(len(recs))
    assert [r["stage"] for r in recs] == ["first", "second"], recs
    assert recs[0]["decision_diff"]["added"] == ["a"], recs[0]
    assert recs[1]["decision_diff"]["added"] == ["b"], recs[1]
    assert [r["approver"] for r in recs] == ["alice", "bob"], \
        "each resume must keep ITS approver — no overwrite: {!r}".format(recs)


async def main() -> None:
    await scenario_resume_emits_projected_record()
    await scenario_decision_values_never_leak()
    await scenario_plain_gate_resume_record()
    await scenario_aggregate_and_pretty_stay_sane()
    await scenario_approver_identity_recorded()
    await scenario_approver_absent_omitted()
    await scenario_gate_diff_escalate()
    await scenario_gate_diff_plain()
    await scenario_gate_diff_bounded()
    await scenario_approver_header_never_flows_downstream()
    await scenario_gate_diff_bounds_key_length()
    await scenario_two_resumes_distinct_diffs()
    print("PASS test_resume_trace")


if __name__ == "__main__":
    asyncio.run(main())
