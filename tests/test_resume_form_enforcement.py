"""Resume-time decision-form enforcement (N1, TIER-0).

A parked HumanGate declares a `form` (the decision shape a driver reads off
`yaah baton-schema`). Before this, `Harness.resume` merged the submitted
decision BLIND — a nonconforming value (`{"decision":"reject"}` against an
`approve`-only form) matched no branch route, fell to the default, and the
run silently misrouted / ended Done. This suite pins the ENFORCEMENT:

  - the four-form matrix (approve / approve_or_revise / free_text / json_schema)
    × (conforming | nonconforming): conforming resumes as before; nonconforming
    is REJECTED with a named `DecisionRejected` (NOT a StageFailed — a rejected
    decision must never arm the ADR-0009 rollback saga);
  - a REJECTED decision leaves the baton RESUMABLE — the operator re-submits a
    conforming decision and the run continues (the raise sits BEFORE _settle, so
    the baton is never evicted);
  - `strict_resume=False` restores the old lenient behavior;
  - a gate with NO form declared is untouched (legacy);
  - an unknown/misconfigured form on a hand-edited baton FAILS LOUD (defense in
    depth — the builder already rejects unknown forms at load time);
  - the error message carries BOTH remedies (baton-schema / strict_resume:false)
    and the structured `to_failure_json` shape carries code/fix_hint/data.

Run: cd yaah && PYTHONPATH=src python3 tests/test_resume_form_enforcement.py

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio

from yaah import Done, Envelope, Graph, Harness, InProcessComms, Kind, Stage, Suspended
from yaah.build.human_gate import HumanGate
from yaah.harness import DecisionRejected, StageFailed


def _gate_harness(form, *, decision_schema=None, routes=None, strict_resume=True):
    """A one-stage pipeline whose only stage is a HumanGate declaring `form`.
    The gate parks the run; resume() delivers the decision. `routes` (optional)
    puts a branch on `decision` so the lenient path can be observed to misroute."""
    comms = InProcessComms()
    comms.register("role:gate", HumanGate(ask="approve?", awaiting="human",
                                          form=form, decision_schema=decision_schema))
    comms.register("role:done", _Echo())
    stages = [Stage("gate", node="role:gate",
                    then=None if routes is None else None,
                    branch={"on": "decision", "routes": routes} if routes else None)]
    if routes:
        stages.append(Stage("done", node="role:done"))
    return Harness(comms, Graph.of(*stages), strict_resume=strict_resume)


class _Echo:
    async def invoke(self, input, config):
        return input.reply("result", **input.payload)


async def _park(harness):
    susp = await harness.run(Envelope(Kind.TASK, {"spec": "x"}))
    assert isinstance(susp, Suspended), susp
    return susp


async def _resume(harness, baton_id, decision):
    return await harness.resume(baton_id, Envelope(Kind.RESUME, dict(decision)))


# --- the four-form matrix: conforming resumes, nonconforming is rejected ------

# each entry: (form, decision_schema, conforming_decision, nonconforming_decision)
_MATRIX = [
    ("approve", None, {"decision": "approve"}, {"decision": "reject"}),
    ("approve_or_revise", None, {"decision": "revise", "feedback": "fix it"},
     {"decision": "approved"}),  # near-miss value the form can't produce
    ("free_text", None, {"answer": "here is my ruling"}, {}),  # missing required 'answer'
    ("json_schema",
     {"type": "object", "properties": {"verdict": {"enum": ["pass", "fail"]}},
      "required": ["verdict"]},
     {"verdict": "pass"}, {"verdict": "maybe"}),  # not in the inline enum
]


async def scenario_matrix_conforming_resumes() -> None:
    for form, dsch, ok, _ in _MATRIX:
        h = _gate_harness(form, decision_schema=dsch)
        susp = await _park(h)
        out = await _resume(h, susp.baton_id, ok)
        assert isinstance(out, Done), (form, out)
        # the conforming decision keys flowed onto the merged output
        for k, v in ok.items():
            assert out.output.payload.get(k) == v, (form, k, out.output.payload)
        # a resumed-to-Done run evicts its baton
        assert await h.batons.list_suspended() == [], form


async def scenario_matrix_nonconforming_rejected() -> None:
    for form, dsch, _, bad in _MATRIX:
        h = _gate_harness(form, decision_schema=dsch)
        susp = await _park(h)
        try:
            await _resume(h, susp.baton_id, bad)
        except DecisionRejected as e:
            # a rejected decision is NOT a StageFailed — it must never reach the
            # ADR-0009 auto-saga (which arms on StageFailed and would unwind
            # priors for a mere input error).
            assert not isinstance(e, StageFailed), (form, "must not subclass StageFailed")
            fj = e.to_failure_json()
            assert fj["outcome"] == "failed", (form, fj)
            f = fj["failures"][0]
            assert f["code"] == "decision_rejected", (form, f)
            assert form in f["message"], (form, "message names the form", f)
            # BOTH remedies present in the message
            assert "baton-schema" in f["message"], (form, "remedy 1 missing", f)
            assert "strict_resume" in f["message"], (form, "remedy 2 missing", f)
            # structured slot carries the form, the schema errors, and the baton id
            assert f["data"]["form"] == form, (form, f)
            assert f["data"]["errors"], (form, "schema errors must be listed", f)
            assert f["data"]["baton_id"] == susp.baton_id, (form, f)
            assert f.get("fix_hint"), (form, "fix_hint present", f)
        else:
            raise AssertionError("{}: nonconforming decision must raise".format(form))


async def scenario_rejected_leaves_baton_resumable() -> None:
    """The headline: a rejected decision does NOT evict the baton. The operator
    re-submits a conforming decision and the run continues."""
    h = _gate_harness("approve_or_revise")
    susp = await _park(h)
    assert len(await h.batons.list_suspended()) == 1

    try:
        await _resume(h, susp.baton_id, {"decision": "banana"})
        raise AssertionError("expected DecisionRejected")
    except DecisionRejected:
        pass

    # the baton is STILL parked and resumable (the raise sat before _settle)
    assert len(await h.batons.list_suspended()) == 1, "rejected decision must NOT evict the baton"

    out = await _resume(h, susp.baton_id, {"decision": "approve"})
    assert isinstance(out, Done), out
    assert await h.batons.list_suspended() == [], "conforming re-submit resumes to Done"


async def scenario_strict_resume_false_is_lenient() -> None:
    """`strict_resume: false` restores the OLD blind-merge behavior: a
    nonconforming decision is accepted and routes on its (dead) value."""
    h = _gate_harness("approve",
                      routes={"approve": None, "reject": "done"},
                      strict_resume=False)
    susp = await _park(h)
    # 'reject' is not in the approve form's enum, but lenient mode accepts it and
    # takes the (otherwise-dead) reject route to `done` — no DecisionRejected.
    out = await _resume(h, susp.baton_id, {"decision": "reject"})
    assert isinstance(out, Done), out
    assert out.output.payload.get("decision") == "reject", out.output


async def scenario_no_form_untouched() -> None:
    """A gate with NO form declared is not validated — legacy behavior."""
    h = _gate_harness(None)  # HumanGate(form=None) stamps no `form` on the AWAIT
    susp = await _park(h)
    # anything goes: an arbitrary decision resumes cleanly, strict_resume ON.
    out = await _resume(h, susp.baton_id, {"whatever": 1})
    assert isinstance(out, Done), out
    assert out.output.payload.get("whatever") == 1, out.output


async def scenario_unknown_form_fails_loud() -> None:
    """Defense in depth: the builder rejects unknown form names at LOAD time, so
    this path only fires on hand-edited baton state. Construct the state directly
    (a parked baton whose pending payload names a bogus form) and assert resume
    fails loud with the SAME family — message says the form itself is invalid."""
    h = _gate_harness("approve")
    susp = await _park(h)
    # hand-edit the persisted baton's pending payload to a form the catalog
    # doesn't know (simulating corrupted / tampered durable state).
    baton = await h.batons.load(susp.baton_id)
    baton.pending.payload["form"] = "bogus_form"
    await h.batons.save(baton)

    try:
        await _resume(h, susp.baton_id, {"decision": "approve"})
        raise AssertionError("expected DecisionRejected for an unknown form")
    except DecisionRejected as e:
        f = e.to_failure_json()["failures"][0]
        assert f["code"] == "decision_rejected", f
        assert "bogus_form" in f["message"], ("message names the bad form", f)
    # the baton stays resumable even on a misconfigured form
    assert len(await h.batons.list_suspended()) == 1


async def scenario_rejection_does_not_arm_saga() -> None:
    """CRITICAL invariant: a rejected decision must NEVER reach the ADR-0009
    auto-saga. `saga.settle_terminal` arms the rollback on any StageFailed;
    DecisionRejected is deliberately NOT one, so it propagates PAST that catch.
    Spy on `run_auto_saga` to prove it is never called for a rejection, even
    though settle_terminal wraps the resume coroutine."""
    import yaah.saga as saga

    called = {"n": 0}

    async def _spy(*a, **k):
        called["n"] += 1
        return None

    orig = saga.run_auto_saga
    saga.run_auto_saga = _spy
    try:
        h = _gate_harness("approve")
        susp = await _park(h)
        # route the resume through settle_terminal exactly as resume_gate does
        try:
            await saga.settle_terminal(
                {}, ".", h,
                h.resume(susp.baton_id, Envelope(Kind.RESUME, {"decision": "nope"})))
            raise AssertionError("expected DecisionRejected to propagate")
        except DecisionRejected:
            pass
        assert called["n"] == 0, "auto-saga must NOT fire on a rejected decision"
    finally:
        saga.run_auto_saga = orig


async def main() -> None:
    await scenario_matrix_conforming_resumes()
    await scenario_matrix_nonconforming_rejected()
    await scenario_rejected_leaves_baton_resumable()
    await scenario_strict_resume_false_is_lenient()
    await scenario_no_form_untouched()
    await scenario_unknown_form_fails_loud()
    await scenario_rejection_does_not_arm_saga()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
