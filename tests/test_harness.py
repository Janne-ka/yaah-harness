"""Harness tests: graph drive, validator retry loop, suspend/resume.

Run: cd yaah && PYTHONPATH=src python3 tests/test_harness.py
"""
from __future__ import annotations

import asyncio

from yaah import (
    Done,
    Envelope,
    Failure,
    Graph,
    Harness,
    InProcessComms,
    NodeConfig,
    Stage,
    StageFailed,
    Suspended,
    Verdict,
)


# --- nodes used across scenarios ---------------------------------------------

class Writer:
    """Returns ok=False on the first try; ok=True once feedback is present."""

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        if input.payload.get("feedback"):
            return input.reply("result", text="FIXED", ok=True)
        return input.reply("result", text="bad", ok=False)


class Stubborn:
    """Never satisfies the validator."""

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply("result", text="nope", ok=False)


class OkValidator:
    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        if input.payload.get("ok"):
            return Verdict.passed().to_envelope()
        return Verdict.failed(Failure("not_ok", "needs ok=true", "set ok=true")).to_envelope()


class SoftFlag:
    """A SOFT validator: flags a concern but does not block."""

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return Verdict.failed(
            Failure("style", "minor nit", "consider X"), severity="soft").to_envelope(input)


class Gate:
    """A node that suspends the run (awaits an external decision)."""

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply("await", awaiting="human")


class Broken:
    """Replies Kind.ERROR — what a remote transport (NatsComms.serve) sends when
    the handler raised. In-proc the exception propagates; over the wire it
    arrives as an envelope, and the harness must treat it as a FAILURE (H3)."""

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply("error", error="RuntimeError('boom')")


class Upper:
    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply("result", text=input.payload["text"].upper())


class Echo:
    """Passes the whole payload through, so a test can inspect what arrived."""

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply("result", **input.payload)


class Exclaim:
    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply("result", text=input.payload["text"] + "!")


# --- scenarios ---------------------------------------------------------------

async def scenario_retry_with_feedback() -> None:
    comms = InProcessComms()
    comms.register("role:writer", Writer())
    comms.register("role:check", OkValidator())
    graph = Graph.of(
        Stage("write", node="role:writer", validators=["role:check"], max_attempts=3, feedback=True)
    )
    outcome = await Harness(comms, graph).run(Envelope("task", {}))
    assert isinstance(outcome, Done), outcome
    assert outcome.output.payload["text"] == "FIXED", outcome.output  # passed on the 2nd attempt


async def scenario_human_gate_suspend_resume() -> None:
    comms = InProcessComms()
    comms.register("role:stubborn", Stubborn())
    comms.register("role:check", OkValidator())
    graph = Graph.of(
        Stage("gate", node="role:stubborn", validators=["role:check"],
              max_attempts=2, feedback=True, escalate="human")
    )
    harness = Harness(comms, graph)

    outcome = await harness.run(Envelope("task", {}))
    assert isinstance(outcome, Suspended), outcome
    assert outcome.awaiting == "human:gate", outcome

    # human approves with a corrected output
    final = await harness.resume(outcome.baton_id, Envelope("result", {"text": "human-approved", "ok": True}))
    assert isinstance(final, Done), final
    assert final.output.payload["text"] == "human-approved", final.output


async def scenario_multistage_handover() -> None:
    comms = InProcessComms()
    comms.register("role:upper", Upper())
    comms.register("role:exclaim", Exclaim())
    graph = Graph.of(
        Stage("a", node="role:upper", then="b"),
        Stage("b", node="role:exclaim"),
    )
    outcome = await Harness(comms, graph).run(Envelope("task", {"text": "hi"}))
    assert isinstance(outcome, Done), outcome
    assert outcome.output.payload["text"] == "HI!", outcome.output


async def scenario_baton_eviction() -> None:
    """Batons are evicted on terminal outcomes (Done / StageFailed); only a
    suspended run keeps its baton, until resume() finishes it. Guards the
    headline memory leak (early_review #1)."""
    comms = InProcessComms()
    comms.register("role:upper", Upper())
    comms.register("role:stubborn", Stubborn())
    comms.register("role:check", OkValidator())

    # 1. a completed run leaves no baton behind
    h1 = Harness(comms, Graph.of(Stage("a", node="role:upper")))
    await h1.run(Envelope("task", {"text": "hi"}))
    assert await h1.batons.list_suspended() == [], "completed run must evict its baton"

    # 2. a failed run (no human gate) also evicts
    h2 = Harness(comms, Graph.of(
        Stage("g", node="role:stubborn", validators=["role:check"], max_attempts=1)))
    raised = False
    try:
        await h2.run(Envelope("task", {}))
    except StageFailed:
        raised = True
    assert raised and await h2.batons.list_suspended() == [], "failed run must evict its baton"

    # 3. a suspended run KEEPS its baton; resume() then evicts it
    h3 = Harness(comms, Graph.of(
        Stage("g", node="role:stubborn", validators=["role:check"],
              max_attempts=1, escalate="human")))
    susp = await h3.run(Envelope("task", {}))
    assert isinstance(susp, Suspended) and len(await h3.batons.list_suspended()) == 1, \
        "suspended run keeps its baton"
    await h3.resume(susp.baton_id, Envelope("result", {"text": "ok", "ok": True}))
    assert await h3.batons.list_suspended() == [], "resumed-to-done run must evict its baton"

    # 4. resuming an already-finished baton is a clean error, not a silent leak
    try:
        await h3.resume(susp.baton_id, Envelope("result", {"ok": True}))
        raise AssertionError("expected KeyError resuming a finished baton")
    except KeyError as e:
        # the error message must name the next diagnostic the operator should
        # run (`yaah list`) — Y3: was previously "(it finished, expired, or
        # never existed)" which collapsed three causes into one cryptic guess
        msg = str(e)
        assert "yaah list" in msg, msg
        assert "single-shot" in msg, msg


async def scenario_baton_ttl() -> None:
    """A suspended run nobody resumes is swept once older than the TTL — the
    abandoned-human-gate leak. Uses a fake clock so no real waiting."""
    comms = InProcessComms()
    comms.register("role:stubborn", Stubborn())
    comms.register("role:check", OkValidator())
    now = {"t": 1000.0}
    h = Harness(comms, Graph.of(
        Stage("g", node="role:stubborn", validators=["role:check"],
              max_attempts=1, escalate="human")),
        wall_clock=lambda: now["t"])  # TTL runs off the wall clock (H1)

    susp = await h.run(Envelope("task", {}), ttl=600.0)  # ttl is the baton's, set per run
    assert isinstance(susp, Suspended) and len(await h.batons.list_suspended()) == 1

    now["t"] += 300         # within TTL — still resumable
    assert await h.sweep_expired() == []
    assert len(await h.batons.list_suspended()) == 1

    now["t"] += 400         # now 700s > 600s TTL — swept
    evicted = await h.sweep_expired()
    assert evicted == [susp.baton_id] and await h.batons.list_suspended() == [], evicted

    # resuming the abandoned (swept) baton is a clean error
    try:
        await h.resume(susp.baton_id, Envelope("result", {"ok": True}))
        raise AssertionError("expected KeyError resuming an expired baton")
    except KeyError:
        pass


async def scenario_baton_ttl_cross_instance() -> None:
    """H1 regression: `parked_at` must be a WALL-CLOCK value so a SECOND harness
    instance (another process) sharing the store computes expiry correctly. With
    the old monotonic clock, instance B's zero point differed from A's, so a parked
    baton looked either eternally-fresh or instantly-expired. Here two harnesses
    share one store; B (started 'later') sweeps using its own wall clock."""
    from yaah.harness import BatonStore
    from yaah.store import MemoryBackend

    store = MemoryBackend()  # stands in for a shared durable store across processes

    def mk(now_holder):
        comms = InProcessComms()
        comms.register("role:stubborn", Stubborn())
        comms.register("role:check", OkValidator())
        return Harness(comms, Graph.of(
            Stage("g", node="role:stubborn", validators=["role:check"],
                  max_attempts=1, escalate="human")),
            wall_clock=lambda: now_holder["t"], baton_store=BatonStore(store))

    a = mk({"t": 1000.0})                       # instance A parks at wall-time 1000
    susp = await a.run(Envelope("task", {}), ttl=600.0)
    assert isinstance(susp, Suspended)

    # instance B, 500s later (within TTL) — must NOT sweep it
    b_early = mk({"t": 1500.0})
    assert await b_early.sweep_expired() == []
    assert len(await b_early.batons.list_suspended()) == 1

    # instance B, 700s later (past TTL) — must sweep it (parked_at is comparable)
    b_late = mk({"t": 1700.0})
    assert await b_late.sweep_expired() == [susp.baton_id]


async def scenario_soft_concerns() -> None:
    """A soft validator failure doesn't block the run, but is recorded as a
    concern on the final output (early_review #11)."""
    comms = InProcessComms()
    comms.register("role:upper", Upper())
    comms.register("role:soft", SoftFlag())
    graph = Graph.of(
        Stage("a", node="role:upper", validators=["role:soft"], max_attempts=1))
    out = await Harness(comms, graph).run(Envelope("task", {"text": "hi"}))
    assert isinstance(out, Done), out
    assert out.output.payload["text"] == "HI", "soft fail must NOT block the stage"
    concerns = out.output.payload.get("concerns")
    assert concerns and concerns[0]["code"] == "style", concerns
    assert concerns[0]["validator"] == "role:soft" and concerns[0]["stage"] == "a", concerns


async def scenario_soft_concerns_surface_at_gate() -> None:
    """Soft concerns gathered before a gate are surfaced on the Suspended outcome
    (the sceptic-concerns-at-the-human-gate path)."""
    comms = InProcessComms()
    comms.register("role:upper", Upper())
    comms.register("role:soft", SoftFlag())
    comms.register("role:gate", Gate())
    graph = Graph.of(
        Stage("a", node="role:upper", validators=["role:soft"], then="g"),
        Stage("g", node="role:gate"),
    )
    out = await Harness(comms, graph).run(Envelope("task", {"text": "hi"}))
    assert isinstance(out, Suspended), out
    assert out.concerns and out.concerns[0]["code"] == "style", out.concerns


async def scenario_escalate_surfaces_own_soft_concerns() -> None:
    """A stage that ESCALATES to human (hard validator fails) still surfaces its
    OWN soft concerns at the gate — not just concerns from prior passed stages.
    Here one validator flags a soft concern and another hard-fails the stage."""
    comms = InProcessComms()
    comms.register("role:stubborn", Stubborn())   # text="nope", ok=False
    comms.register("role:soft", SoftFlag())       # soft concern, code "style"
    comms.register("role:check", OkValidator())   # hard fail (ok is False)
    graph = Graph.of(
        Stage("gate", node="role:stubborn", validators=["role:soft", "role:check"],
              max_attempts=1, escalate="human"))
    out = await Harness(comms, graph).run(Envelope("task", {}))
    assert isinstance(out, Suspended), out
    assert out.awaiting == "human:gate", out
    codes = [c["code"] for c in out.concerns]
    assert "style" in codes, out.concerns  # the escalating stage's own soft concern


async def scenario_resume_merges_artifact() -> None:
    """On escalate→human, resume merges the decision onto the FAILED stage's
    artifact — downstream sees both, not just the decision (early_review #18)."""
    comms = InProcessComms()
    comms.register("role:stubborn", Stubborn())  # outputs text="nope", ok=False
    comms.register("role:check", OkValidator())
    comms.register("role:after", Echo())
    graph = Graph.of(
        Stage("g", node="role:stubborn", validators=["role:check"],
              max_attempts=1, escalate="human", then="after"),
        Stage("after", node="role:after"),
    )
    h = Harness(comms, graph)
    susp = await h.run(Envelope("task", {}))
    assert isinstance(susp, Suspended), susp

    final = await h.resume(susp.baton_id, Envelope("result", {"decision": "approved"}))
    assert isinstance(final, Done), final
    assert final.output.payload["text"] == "nope", "failed artifact must survive resume"
    assert final.output.payload["decision"] == "approved", "human decision must be merged in"


async def scenario_error_reply_fails_validatorless_stage() -> None:
    """H3: a Kind.ERROR reply (remote handler raised, surfaced by the transport
    as an envelope) must FAIL the stage — before this fix a validator-less stage
    validated it as Verdict.passed() and the run sailed on with an error payload
    as its 'artifact'. The StageFailed message must NAME the error (theme C:
    failure info travels)."""
    comms = InProcessComms()
    comms.register("role:broken", Broken())
    h = Harness(comms, Graph.of(Stage("s", node="role:broken")))  # NO validators
    try:
        await h.run(Envelope("task", {}))
        raise AssertionError("Kind.ERROR reply must not complete as Done")
    except StageFailed as e:
        assert e.verdict.failures and e.verdict.failures[0].code == "node_error", e.verdict
        assert "boom" in str(e), "the error detail must travel into the message: " + str(e)


async def scenario_error_reply_in_fanout_is_a_failed_role() -> None:
    """H3 fan-out side: one role replying Kind.ERROR counts as a FAILED role
    (failed_roles populated, ready fanout_error verdict) instead of being merged
    as a result — over NATS this list was always empty."""
    comms = InProcessComms()
    comms.register("role:upper", Upper())
    comms.register("role:broken", Broken())
    h = Harness(comms, Graph.of(
        Stage("fan", node="role:upper", fanout=["role:upper", "role:broken"])))
    try:
        await h.run(Envelope("task", {"text": "hi"}))
        raise AssertionError("a failed fan-out role must fail the stage")
    except StageFailed as e:
        assert e.output is not None
        assert e.output.payload["failed_roles"] == ["role:broken"], e.output.payload
        assert e.output.payload["roles"] == ["role:upper"], "healthy roles must survive"


async def scenario_validator_error_reply_hard_fails_with_detail() -> None:
    """H3 validator side: the VALIDATOR crashing remotely (its reply is
    Kind.ERROR) is a hard fail carrying the actual error — not an empty
    malformed-verdict fail with no detail."""
    comms = InProcessComms()
    comms.register("role:upper", Upper())
    comms.register("role:vbroken", Broken())
    h = Harness(comms, Graph.of(
        Stage("s", node="role:upper", validators=["role:vbroken"], max_attempts=1)))
    try:
        await h.run(Envelope("task", {"text": "hi"}))
        raise AssertionError("a crashed validator must fail the stage")
    except StageFailed as e:
        assert e.verdict.failures[0].code == "node_error", e.verdict
        assert "boom" in str(e), str(e)


class Sceptic:
    """A stage whose OUTPUT carries payload-borne concerns (a parsed sceptic
    report) — the `concerns_from` source."""

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply(
            "result", text="spec looks plausible",
            sceptic_concerns=[
                {"code": "spec_drift", "message": "AC-2 has no covering test", "fix_hint": "add one"},
                "bare string concern",
            ])


async def scenario_concerns_from_surface_at_gate() -> None:
    # TODO 882: a sceptic stage's payload concerns reach the NEXT human gate via
    # the engine channel — surviving an intermediate stage that carries nothing.
    comms = InProcessComms()
    comms.register("role:sceptic", Sceptic())
    comms.register("role:upper", Upper())
    comms.register("role:gate", Gate())
    graph = Graph.of(
        Stage("sceptic", node="role:sceptic", concerns_from="sceptic_concerns", then="mid"),
        Stage("mid", node="role:upper", then="gate"),  # does NOT carry the key onward
        Stage("gate", node="role:gate"),
    )
    outcome = await Harness(comms, graph).run(Envelope("task", {"text": "x"}))
    assert isinstance(outcome, Suspended), outcome
    codes = [c["code"] for c in outcome.concerns]
    assert "spec_drift" in codes and "concern" in codes, outcome.concerns
    msgs = " ".join(c["message"] for c in outcome.concerns)
    assert "AC-2 has no covering test" in msgs and "bare string concern" in msgs
    assert all(c["stage"] == "sceptic" for c in outcome.concerns), outcome.concerns


async def scenario_stage_span_records_shell_exit_code() -> None:
    """Error-path contract (BUG-662 class): a shell stage's exit code must be
    observable in its `stage` trace span even on the PASS path — ShellNode
    never fails the stage, so a tolerated non-zero exit would otherwise leave
    no trace at all (the `|| true` blindness that kept a dead extractor
    invisible for ~3 weeks)."""
    from yaah.nodes import ShellNode
    from yaah.trace import RecordingTracer

    comms = InProcessComms()
    comms.register("role:sh", ShellNode(["false"]))
    tr = RecordingTracer()
    outcome = await Harness(comms, Graph.of(Stage("sh", node="role:sh")),
                            tracer=tr).run(Envelope("task", {}))
    assert isinstance(outcome, Done), outcome
    spans = [s for s in tr.spans if s.name == "stage"]
    assert spans and spans[0].attrs.get("exit_code") == 1, [
        (s.name, s.attrs) for s in tr.spans]


async def main() -> None:
    await scenario_retry_with_feedback()
    await scenario_human_gate_suspend_resume()
    await scenario_resume_merges_artifact()
    await scenario_multistage_handover()
    await scenario_baton_eviction()
    await scenario_baton_ttl()
    await scenario_baton_ttl_cross_instance()
    await scenario_soft_concerns()
    await scenario_soft_concerns_surface_at_gate()
    await scenario_concerns_from_surface_at_gate()
    await scenario_escalate_surfaces_own_soft_concerns()
    await scenario_error_reply_fails_validatorless_stage()
    await scenario_error_reply_in_fanout_is_a_failed_role()
    await scenario_validator_error_reply_hard_fails_with_detail()
    await scenario_stage_span_records_shell_exit_code()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
