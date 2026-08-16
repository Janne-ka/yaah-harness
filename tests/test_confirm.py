"""Confirm beat — the SECOND-OPINION check at a stage's done-boundary.

The failure class: an agentic loop exits at the FIRST plausible-looking success —
a node whose output passes its deterministic validators is treated as "task done"
and the harness advances, even when the output is valid-but-wrong. The confirm beat
is a structural "halt and check" at the done-boundary: after the deterministic
validators PASS but before the Pass is committed, a cheap COLD checker agent reads
the OUTPUT + the task and can veto. A veto (ok=false) is mapped onto a failed
Verdict, so it re-enters the EXISTING retry-with-feedback / escalate / StageFailed
loop — no parallel machinery.

Each scenario asserts one of the five non-negotiable properties:
  1. CHEAP        scenario_confirm_uses_its_own_cheap_model
  2. COLD         scenario_confirm_is_cold_no_producer_scratch
  3. ADVERSARIAL  (prompt is app-supplied; the engine only names the role —
                  scenario_validate_confirm_wiring proves the role is dispatched
                  as a normal node, its model/prompt its own config)
  4. BOUNDED      scenario_confirm_always_rejects_terminates_at_max_attempts
  5. OPT-IN       scenario_no_confirm_is_byte_identical
plus the planted premature-done + control cases (a/b) and the build-time wiring.

Run: cd yaah && PYTHONPATH=src python3 tests/test_confirm.py
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
from yaah.validate import validate_pipeline


# --- nodes -------------------------------------------------------------------

class Producer:
    """The producing agent. Records its call count and the feedback it saw, so a
    test can prove the confirm reason came back as retry feedback. `fixable`:
    when True it emits the "correct" answer once feedback is present (models an
    agent that self-corrects); when False it is stubborn (always "wrong")."""

    def __init__(self, fixable: bool = False, model: str = "claude:sonnet") -> None:
        self.calls = 0
        self.seen_feedback = []  # the feedback payloads the harness folded in
        self._fixable = fixable
        self._model = model

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        self.calls += 1
        fb = input.payload.get("feedback")
        if fb:
            self.seen_feedback.append(fb)
        answer = "correct" if (self._fixable and fb) else "wrong"
        # explicit output — does NOT echo the whole input, so the harness-injected
        # priorAttempt/feedback scratch stays OUT of the producer's OUTPUT.
        return input.reply("result", answer=answer)


class NeedsFinal:
    """A DETERMINISTIC validator that passes iff answer == 'final'. Used only by
    the COLD scenario to force ONE feedback retry (so priorAttempt/feedback
    scratch exists on the passing attempt's input)."""

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        if input.payload.get("answer") == "final":
            return Verdict.passed().to_envelope(input)
        return Verdict.failed(Failure(
            "not_final", "answer must be 'final'", "emit final")).to_envelope(input)


class HasAnswer:
    """A DETERMINISTIC validator that passes whenever an `answer` key is present —
    so a valid-but-WRONG output (answer='wrong') sails through it. This is the
    premature-done premise: the deterministic gate cannot tell right from wrong."""

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        if "answer" in input.payload:
            return Verdict.passed().to_envelope(input)
        return Verdict.failed(Failure(
            "no_answer", "need an answer", "produce one")).to_envelope(input)


class DraftThenFinal:
    """Producer for the COLD case: emits answer='draft' until feedback arrives,
    then answer='final'. The one validator failure injects priorAttempt/feedback
    into the second attempt's input — the producer scratch the confirmer must not
    see."""

    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        self.calls += 1
        answer = "final" if input.payload.get("feedback") else "draft"
        return input.reply("result", answer=answer)


class ConfirmChecker:
    """The second-opinion checker AGENT. Returns the {ok, reason} contract in its
    result payload (the engine maps it onto a Verdict). Records every payload it
    received (COLD assertions), its call count (dispatch-count assertions), and the
    model the engine passed through in its config (CHEAP assertion). `accept` is a
    predicate over the received payload; `reason` is emitted on rejection."""

    def __init__(self, accept, reason: str = "answer is wrong") -> None:
        self._accept = accept
        self._reason = reason
        self.calls = 0
        self.seen = []      # payloads received (for the cold-boundary assertion)
        self.model = None   # config.model the engine threaded in (cheap-model proof)

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        self.calls += 1
        self.seen.append(dict(input.payload))
        self.model = config.model
        if self._accept(input.payload):
            return input.reply("result", ok=True, reason="looks right")
        return input.reply("result", ok=False, reason=self._reason)


class RawConfirm:
    """A confirm node that returns an AUTHOR-SPECIFIED reply — an arbitrary payload
    (missing/non-bool `ok`) or a Kind.ERROR (provider down). Exercises the fail-safe
    mapping: a broken confirmer must never wave the output through."""

    def __init__(self, payload=None, error=None) -> None:
        self.calls = 0
        self._payload = payload
        self._error = error

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        self.calls += 1
        if self._error is not None:
            return input.reply("error", error=self._error)
        return input.reply_with("result", dict(self._payload or {}))


async def _noop(*_a, **_k) -> None:  # injected for self._sleep so a REGRESSION can't wait
    return None


def _correct(p) -> bool:
    return p.get("answer") == "correct"


# --- (a) PLANTED PREMATURE-DONE ----------------------------------------------

async def scenario_premature_done_retries_then_escalates() -> None:
    """The core case. A stubborn producer emits a VALID-but-WRONG output that the
    deterministic validator passes; the confirm rejects it (ok=false). The harness
    must NOT advance: it retries with the confirm REASON as feedback, spends its
    attempts, and on exhaustion escalates — the parked artifact carrying the confirm
    reason. Proves the beat blocks a premature done AND reuses the retry/escalate
    loop (reason -> feedback -> escalation)."""
    comms = InProcessComms()
    producer = Producer(fixable=False)
    confirm = ConfirmChecker(accept=_correct, reason="answer is wrong")
    comms.register("role:make", producer)
    comms.register("role:has", HasAnswer())
    comms.register("role:confirm", confirm)
    graph = Graph.of(
        # terminal escalate: the parked artifact IS the run's last word, so the
        # merged resume output still carries the confirm reason we assert on.
        Stage("s", node="role:make", validators=["role:has"], confirm="role:confirm",
              max_attempts=2, feedback=True, escalate="human"),
    )
    h = Harness(comms, graph)
    out = await h.run(Envelope("task", {}))

    # did NOT advance on the first plausible-done: it retried, then parked
    assert isinstance(out, Suspended), out
    assert out.awaiting == "human:s", out
    assert producer.calls == 2, ("must retry, not advance", producer.calls)
    assert confirm.calls == 2, confirm.calls
    # the confirm REASON came back as feedback on the retry (loop reuse)
    assert producer.seen_feedback, "confirm reason must feed the retry loop"
    assert any(f["code"] == "confirm_rejected" and "answer is wrong" in f["message"]
               for f in producer.seen_feedback[0]), producer.seen_feedback

    # the PARKED artifact carries the confirm reason (survives resume/merge)
    final = await h.resume(out.baton_id, Envelope("result", {"decision": "approved"}))
    assert isinstance(final, Done), final
    esc = final.output.payload.get("escalation")
    assert esc and esc["stage"] == "s", esc
    codes = [f["code"] for f in esc["failures"]]
    assert "confirm_rejected" in codes, esc
    assert any("answer is wrong" in f["message"] for f in esc["failures"]), esc


async def scenario_confirm_reason_feeds_recovery() -> None:
    """The reason-as-feedback loop is not just cosmetic: a producer that self-
    corrects on feedback RECOVERS through the same path (attempt 2 passes the
    confirm) — proving the veto is 'treated exactly like a validator failure'."""
    comms = InProcessComms()
    producer = Producer(fixable=True)
    confirm = ConfirmChecker(accept=_correct)
    comms.register("role:make", producer)
    comms.register("role:has", HasAnswer())
    comms.register("role:confirm", confirm)
    graph = Graph.of(
        Stage("s", node="role:make", validators=["role:has"], confirm="role:confirm",
              max_attempts=3, feedback=True))
    out = await Harness(comms, graph).run(Envelope("task", {}))
    assert isinstance(out, Done), out
    assert out.output.payload["answer"] == "correct", out.output.payload
    assert producer.calls == 2 and confirm.calls == 2, (producer.calls, confirm.calls)


async def scenario_veto_reason_mentioning_infra_still_spends_attempt() -> None:
    """HIGH regression (cold review): a confirm VETO whose agent-authored reason
    merely MENTIONS an infra word ('timeout', 'rate limit') must NOT be misread as a
    transient blip. If it were, it would ride the separate error_retries budget —
    spending NO max_attempts attempt and folding NO feedback — silently tripling the
    producer's cost and suppressing recovery. Pin that a substantive veto spends a
    REAL attempt, folds the reason as feedback, and parks at max_attempts."""
    comms = InProcessComms()
    producer = Producer(fixable=False)
    confirm = ConfirmChecker(
        accept=lambda p: False,
        reason="the summary is wrong: it invented a timeout and a rate limit that "
               "the source never mentions")
    comms.register("role:make", producer)
    comms.register("role:has", HasAnswer())
    comms.register("role:confirm", confirm)
    graph = Graph.of(
        Stage("s", node="role:make", validators=["role:has"], confirm="role:confirm",
              max_attempts=2, feedback=True, escalate="human"))
    h = Harness(comms, graph)
    h._sleep = _noop  # a regression (misclassified transient) would sleep the backoff
    out = await h.run(Envelope("task", {}))

    assert isinstance(out, Suspended), out
    # EXACTLY max_attempts producer calls: a misclassified-transient veto would burn
    # the error_retries budget first and run the producer many more times.
    assert producer.calls == 2, ("infra-word veto must spend real attempts, not the "
                                 "error budget", producer.calls)
    assert confirm.calls == 2, confirm.calls
    # feedback WAS folded (the transient branch skips _with_feedback)
    assert producer.seen_feedback, "a real veto must fold its reason as feedback"
    assert any(f["code"] == "confirm_rejected" for f in producer.seen_feedback[0]), \
        producer.seen_feedback


async def scenario_confirm_dispatch_is_traced() -> None:
    """MED (cold review): every confirm dispatch emits one `confirm` trace note —
    pass OR veto — so the second agent's cost/latency is never hidden on the happy
    path (the surprise the CHEAP property invites)."""
    from yaah.trace import RecordingTracer

    # happy path: a confirm note with ok=True
    comms = InProcessComms()
    comms.register("role:make", Producer(fixable=False))
    comms.register("role:has", HasAnswer())
    comms.register("role:confirm", ConfirmChecker(accept=lambda p: True))
    tr = RecordingTracer()
    await Harness(comms, Graph.of(
        Stage("s", node="role:make", validators=["role:has"], confirm="role:confirm")),
        tracer=tr).run(Envelope("task", {}))
    notes = [s for s in tr.spans if s.attrs.get("confirm") == "role:confirm"]
    assert notes and notes[0].attrs["ok"] is True, [(s.name, s.attrs) for s in tr.spans]

    # veto path: a confirm note with ok=False, distinguishable in the trace
    comms2 = InProcessComms()
    comms2.register("role:make", Producer(fixable=False))
    comms2.register("role:has", HasAnswer())
    comms2.register("role:confirm", ConfirmChecker(accept=lambda p: False, reason="nope"))
    tr2 = RecordingTracer()
    try:
        await Harness(comms2, Graph.of(
            Stage("s", node="role:make", validators=["role:has"], confirm="role:confirm",
                  max_attempts=1)), tracer=tr2).run(Envelope("task", {}))
    except StageFailed:
        pass
    vetoes = [s for s in tr2.spans if s.attrs.get("confirm") == "role:confirm"]
    assert vetoes and vetoes[0].attrs["ok"] is False, [(s.name, s.attrs) for s in tr2.spans]


# --- fail-safe: a broken confirmer must never wave output through ---------------

async def scenario_confirm_provider_error_does_not_advance() -> None:
    """The confirm agent replies Kind.ERROR (provider down). It must NOT advance —
    it fails loud through the loop (code node_error), producer output not committed."""
    comms = InProcessComms()
    producer = Producer(fixable=False)
    confirm = RawConfirm(error="RuntimeError('confirm provider down')")
    comms.register("role:make", producer)
    comms.register("role:has", HasAnswer())
    comms.register("role:confirm", confirm)
    graph = Graph.of(
        Stage("s", node="role:make", validators=["role:has"], confirm="role:confirm",
              max_attempts=1))
    raised = False
    try:
        await Harness(comms, graph).run(Envelope("task", {}))
    except StageFailed as e:
        raised = True
        assert e.verdict.failures[0].code == "node_error", e.verdict
    assert raised, "an ERROR-replying confirm must not complete as Done"
    assert producer.calls == 1 and confirm.calls == 1, (producer.calls, confirm.calls)


async def scenario_confirm_missing_or_nonbool_ok_is_malformed() -> None:
    """A confirm reply missing `ok`, or with a non-bool `ok`, is a MISCONFIGURATION —
    mapped to confirm_malformed and routed through the loop, never silently a pass."""
    for bad_payload in ({"reason": "no ok key"}, {"ok": "yes", "reason": "stringy"}):
        comms = InProcessComms()
        producer = Producer(fixable=False)
        confirm = RawConfirm(payload=bad_payload)
        comms.register("role:make", producer)
        comms.register("role:has", HasAnswer())
        comms.register("role:confirm", confirm)
        graph = Graph.of(
            Stage("s", node="role:make", validators=["role:has"], confirm="role:confirm",
                  max_attempts=1))
        raised = False
        try:
            await Harness(comms, graph).run(Envelope("task", {}))
        except StageFailed as e:
            raised = True
            assert e.verdict.failures[0].code == "confirm_malformed", (bad_payload, e.verdict)
        assert raised, ("a malformed confirm reply must not complete as Done", bad_payload)
        assert producer.calls == 1, (bad_payload, producer.calls)


# --- (b) CONTROL --------------------------------------------------------------

async def scenario_control_confirm_ok_advances_no_extra_attempts() -> None:
    """A correct output + confirm ok=true advances with NO extra attempts — the
    common path costs exactly one producer call and one confirm call."""
    comms = InProcessComms()
    producer = Producer(fixable=True)  # emits "correct" when fed; here never fed
    # accept the first output directly (answer=='wrong' but the checker OKs it) so
    # the control path is unambiguous: confirm passes on attempt 1.
    confirm = ConfirmChecker(accept=lambda p: True)
    comms.register("role:make", producer)
    comms.register("role:has", HasAnswer())
    comms.register("role:confirm", confirm)
    graph = Graph.of(
        Stage("s", node="role:make", validators=["role:has"], confirm="role:confirm",
              max_attempts=3, feedback=True))
    out = await Harness(comms, graph).run(Envelope("task", {}))
    assert isinstance(out, Done), out
    assert producer.calls == 1, ("no extra attempts on the happy path", producer.calls)
    assert confirm.calls == 1, confirm.calls


# --- (c) COLD -----------------------------------------------------------------

async def scenario_confirm_is_cold_no_producer_scratch() -> None:
    """The confirmer's context is the OUTPUT + the pristine TASK, constructible
    from the envelope alone — NEVER the producer's retry scratch. Here one
    deterministic validator failure forces a feedback retry, so the PASSING
    attempt's producer input carries priorAttempt (the producer's rejected draft)
    and feedback (the validator critique) — producer-private reasoning that lives
    in the node's SCRATCH but not its OUTPUT. Assert the confirmer never sees it,
    while it DOES see the output and the planted task field."""
    comms = InProcessComms()
    producer = DraftThenFinal()
    confirm = ConfirmChecker(accept=lambda p: True)  # accept, so the run completes
    comms.register("role:make", producer)
    comms.register("role:final", NeedsFinal())
    comms.register("role:confirm", confirm)
    graph = Graph.of(
        Stage("s", node="role:make", validators=["role:final"], confirm="role:confirm",
              max_attempts=3, feedback=True))
    # `task_marker` is a pristine TASK field the cold context must carry forward.
    out = await Harness(comms, graph).run(Envelope("task", {"task_marker": "T"}))
    assert isinstance(out, Done), out
    assert producer.calls == 2, ("one validator retry expected", producer.calls)
    assert confirm.calls == 1, ("confirm runs once, after validators pass", confirm.calls)

    seen = confirm.seen[0]
    # OUTPUT visible + TASK visible
    assert seen.get("answer") == "final", seen
    assert seen.get("task_marker") == "T", ("pristine task context must reach the confirmer", seen)
    # PRODUCER SCRATCH invisible — the whole point of the cold boundary
    assert "priorAttempt" not in seen, ("producer's rejected draft leaked", seen)
    assert "feedback" not in seen, ("validator critique / retry scratch leaked", seen)


# --- (d) BOUNDED --------------------------------------------------------------

async def scenario_confirm_always_rejects_terminates_at_max_attempts() -> None:
    """A confirm that ALWAYS returns ok=false must TERMINATE at max_attempts, never
    loop forever. Two terminations, both bounded by the existing loop:
      - no escalate  -> StageFailed after exactly max_attempts producer calls
      - escalate     -> Suspended (parked) after exactly max_attempts, carrying the reason
    """
    # no-escalate -> StageFailed, bounded
    comms = InProcessComms()
    producer = Producer(fixable=False)
    confirm = ConfirmChecker(accept=lambda p: False, reason="never good enough")
    comms.register("role:make", producer)
    comms.register("role:has", HasAnswer())
    comms.register("role:confirm", confirm)
    graph = Graph.of(
        Stage("s", node="role:make", validators=["role:has"], confirm="role:confirm",
              max_attempts=3, feedback=True))
    raised = False
    try:
        await Harness(comms, graph).run(Envelope("task", {}))
    except StageFailed as e:
        raised = True
        assert e.verdict.failures[0].code == "confirm_rejected", e.verdict
        assert "never good enough" in str(e), str(e)
    assert raised, "an always-rejecting confirm with no escalate must StageFailed"
    assert producer.calls == 3, ("terminate at max_attempts, not spin", producer.calls)

    # escalate -> Suspended (parks), bounded
    comms2 = InProcessComms()
    producer2 = Producer(fixable=False)
    confirm2 = ConfirmChecker(accept=lambda p: False, reason="never good enough")
    comms2.register("role:make", producer2)
    comms2.register("role:has", HasAnswer())
    comms2.register("role:confirm", confirm2)
    graph2 = Graph.of(
        Stage("s", node="role:make", validators=["role:has"], confirm="role:confirm",
              max_attempts=3, feedback=True, escalate="human"))
    out = await Harness(comms2, graph2).run(Envelope("task", {}))
    assert isinstance(out, Suspended), out
    assert producer2.calls == 3, producer2.calls


# --- (e) OPT-IN ---------------------------------------------------------------

async def scenario_no_confirm_is_byte_identical() -> None:
    """No `confirm` declared -> the confirm node is NEVER dispatched and behavior is
    exactly today's: a valid-but-wrong output passes the deterministic validator and
    the run advances in a single attempt. Also re-runs an EXISTING harness scenario
    to prove the shared loop is unchanged for confirm-less stages."""
    comms = InProcessComms()
    producer = Producer(fixable=False)
    confirm = ConfirmChecker(accept=lambda p: False)  # registered but must never run
    comms.register("role:make", producer)
    comms.register("role:has", HasAnswer())
    comms.register("role:confirm", confirm)
    graph = Graph.of(
        Stage("s", node="role:make", validators=["role:has"],  # NO confirm
              max_attempts=3, feedback=True))
    out = await Harness(comms, graph).run(Envelope("task", {}))
    assert isinstance(out, Done), out
    assert out.output.payload["answer"] == "wrong", "premature done is TODAY's behavior"
    assert producer.calls == 1, producer.calls
    assert confirm.calls == 0, ("no dispatch without a confirm declaration", confirm.calls)

    # an existing retry-with-feedback scenario still passes unchanged
    from test_harness import scenario_retry_with_feedback
    await scenario_retry_with_feedback()


# --- (1) CHEAP ----------------------------------------------------------------

async def scenario_confirm_uses_its_own_cheap_model() -> None:
    """The confirm declares its own model — a cheap/haiku-class one, SEPARATE from
    the producing node's — and the engine passes it through (dispatch is a normal
    comms.request to the confirm ROLE; the model rides that node's config). The
    engine hardcodes nothing: it never substitutes the producer's model."""
    comms = InProcessComms()
    producer = Producer(fixable=False, model="claude:sonnet")
    confirm = ConfirmChecker(accept=lambda p: True)
    comms.register("role:make", producer, NodeConfig(model="claude:sonnet"))
    comms.register("role:has", HasAnswer())
    comms.register("role:confirm", confirm, NodeConfig(model="claude:haiku"))
    graph = Graph.of(
        Stage("s", node="role:make", validators=["role:has"], confirm="role:confirm"))
    out = await Harness(comms, graph).run(Envelope("task", {}))
    assert isinstance(out, Done), out
    assert confirm.calls == 1, confirm.calls
    # the checker ran under ITS OWN declared (cheap) model, not the producer's
    assert confirm.model == "claude:haiku", confirm.model
    assert confirm.model != producer._model, "confirm model must be independent"


# --- build-time wiring (schema support) --------------------------------------

def scenario_validate_confirm_wiring() -> None:
    """validate.py support: confirm names a declared node (like a validator); a typo
    or an undeclared role fails loud; confirm on a fork/fanin stage is rejected (the
    beat never runs there)."""
    def pipe(stage_extra, extra_nodes=None):
        nodes = {"x": {"type": "transform", "target": "fn:m:f"},
                 "chk": {"type": "agent", "prompt": "ok?", "model": "fake:ok"}}
        if extra_nodes:
            nodes.update(extra_nodes)
        return {"nodes": nodes,
                "graph": {"start": "s1",
                          "stages": {"s1": dict({"node": "x"}, **stage_extra)}}}

    # valid: confirm resolves to a declared node
    validate_pipeline(pipe({"confirm": "chk"}))

    # undeclared confirm role -> loud
    try:
        validate_pipeline(pipe({"confirm": "nope"}))
        raise AssertionError("undeclared confirm role must be rejected")
    except ValueError as e:
        assert "confirm role 'nope' is not a declared node" in str(e), str(e)

    # a non-agent (transform) confirmer -> loud (can't return {ok, reason})
    try:
        validate_pipeline(pipe({"confirm": "x"}))  # x is the transform node
        raise AssertionError("a non-agent confirm role must be rejected")
    except ValueError as e:
        assert "confirm must be an `agent` node" in str(e), str(e)

    # empty / non-string confirm -> loud
    try:
        validate_pipeline(pipe({"confirm": ""}))
        raise AssertionError("empty confirm must be rejected")
    except ValueError as e:
        assert "confirm must be a non-empty role string" in str(e), str(e)

    # confirm on a fanin stage -> rejected (never runs through the attempt loop)
    fanin = {"nodes": {"x": {"type": "transform", "target": "fn:m:f"},
                       "chk": {"type": "agent", "prompt": "ok?", "model": "fake:ok"}},
             "graph": {"start": "j", "stages": {
                 "j": {"fanin": {"expect": ["j"]}, "confirm": "chk"}}}}
    try:
        validate_pipeline(fanin)
        raise AssertionError("confirm on a fanin stage must be rejected")
    except ValueError as e:
        assert "confirm is not allowed on a fork/fanin stage" in str(e), str(e)


async def main() -> None:
    await scenario_premature_done_retries_then_escalates()
    await scenario_confirm_reason_feeds_recovery()
    await scenario_veto_reason_mentioning_infra_still_spends_attempt()
    await scenario_confirm_dispatch_is_traced()
    await scenario_confirm_provider_error_does_not_advance()
    await scenario_confirm_missing_or_nonbool_ok_is_malformed()
    await scenario_control_confirm_ok_advances_no_extra_attempts()
    await scenario_confirm_is_cold_no_producer_scratch()
    await scenario_confirm_always_rejects_terminates_at_max_attempts()
    await scenario_no_confirm_is_byte_identical()
    await scenario_confirm_uses_its_own_cheap_model()
    scenario_validate_confirm_wiring()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
