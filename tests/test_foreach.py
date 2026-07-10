"""foreach (ADR-0007) — dynamic per-item fan-out: bounded map of one node over a
runtime list. These FALSIFY the spec: per-item input is REPLACE + named carries +
sticky (never a full payload copy), results are {item_index, payload} pairs in
item order (survive compaction), corr is preserved per item (trace stitching),
the concurrency bound holds, failures classify exactly like fanout (ERROR /
failed-VERDICT / exception → failed_items; min_success k-of-n), AWAIT parks the
whole stage, and bad items input fails loud.

Run: cd yaah && PYTHONPATH=src python3 tests/test_foreach.py
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
    Kind,
    NodeConfig,
    Stage,
    StageFailed,
    Suspended,
    Verdict,
)


# --- nodes ---------------------------------------------------------------------------------

class Echo:
    """Per-item worker double: replies with the item it saw, uppercased, and
    RECORDS every input payload + headers so tests can assert the per-item
    envelope contract."""

    def __init__(self) -> None:
        self.inputs: list = []
        self.corrs: list = []

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        self.inputs.append(dict(input.payload))
        self.corrs.append(input.correlation_id)
        item = input.payload.get("item")
        return input.reply_with(Kind.RESULT, {"upper": str(item).upper()})


class SlowEcho(Echo):
    """Echo with a small await + concurrency tracking, to probe the bound and
    order preservation under out-of-order completion."""

    def __init__(self, delays=None) -> None:
        super().__init__()
        self.active = 0
        self.peak = 0
        self._delays = delays or {}

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self._delays.get(input.payload.get("item"), 0.001))
            return await super().invoke(input, config)
        finally:
            self.active -= 1


class FailOn:
    """Fails (raises / ERROR / failed VERDICT / AWAIT, per mode) for one item."""

    def __init__(self, bad_item: str, mode: str = "raise") -> None:
        self.bad, self.mode = bad_item, mode

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        item = input.payload.get("item")
        if item == self.bad:
            if self.mode == "raise":
                raise RuntimeError("boom on {}".format(item))
            if self.mode == "error":
                return input.reply_with(Kind.ERROR, {"error": "handler raised"})
            if self.mode == "verdict":
                return Verdict.failed(Failure(
                    "schema_mismatch", "bad item output", "fix it")).to_envelope(input)
            if self.mode == "await":
                return input.reply_with(Kind.AWAIT, {"awaiting": "human:item"})
        return input.reply_with(Kind.RESULT, {"upper": str(item).upper()})


def _graph(foreach: dict, *, min_success=None, sticky=None) -> Graph:
    g = Graph.of(Stage("swarm", node="role:worker", foreach=foreach,
                       min_success=min_success))
    if sticky:
        g.sticky = list(sticky)
    return g


def _harness(node, foreach, **kw) -> Harness:
    comms = InProcessComms()
    comms.register("role:worker", node)
    return Harness(comms, _graph(foreach, **kw))


# --- happy path ------------------------------------------------------------------------------

async def scenario_maps_items_in_order_as_index_payload_pairs() -> None:
    node = Echo()
    h = _harness(node, {"items": "reqs"})
    out = await h.run(Envelope("task", {"reqs": ["a", "b", "c"], "doc": "BIG"}))
    assert isinstance(out, Done), out
    p = out.output.payload
    assert p["failed_items"] == [], p
    assert [r["item_index"] for r in p["results"]] == [0, 1, 2], p["results"]
    assert [r["payload"]["upper"] for r in p["results"]] == ["A", "B", "C"], p["results"]
    assert p["doc"] == "BIG" and p["reqs"] == ["a", "b", "c"], "inbound keys must survive the merge"


async def scenario_per_item_input_is_replace_plus_carries_and_sticky() -> None:
    # THE cost contract (design-eval #1/#7): the per-item payload is exactly
    # {into, item_index} + named carries + sticky — NOT the whole inbound.
    node = Echo()
    h = _harness(node, {"items": "reqs", "into": "item", "carry": ["doc"]},
                 sticky=["workdir"])
    out = await h.run(Envelope("task", {"reqs": ["a", "b"], "doc": "D",
                                        "huge": "X" * 100, "workdir": "/wt"}))
    assert isinstance(out, Done), out
    for i, seen in enumerate(sorted(node.inputs, key=lambda d: d["item_index"])):
        assert seen == {"item": ["a", "b"][i], "item_index": i,
                        "doc": "D", "workdir": "/wt"}, seen  # no "huge", no "reqs"


async def scenario_per_item_corr_is_the_run_corr() -> None:
    # design-eval #3: a bare Envelope would mint a fresh corr per item and orphan
    # every item's trace from the run. reply_with preserves it.
    node = Echo()
    h = _harness(node, {"items": "reqs"})
    task = Envelope("task", {"reqs": ["a", "b"]})
    out = await h.run(task)
    assert isinstance(out, Done), out
    assert set(node.corrs) == {task.correlation_id}, node.corrs


async def scenario_custom_into_key() -> None:
    node = Echo()
    h = _harness(node, {"items": "reqs", "into": "req"})
    out = await h.run(Envelope("task", {"reqs": ["z"]}))
    assert isinstance(out, Done)
    assert node.inputs[0]["req"] == "z" and "item" not in node.inputs[0], node.inputs


# --- concurrency + ordering ------------------------------------------------------------------

async def scenario_concurrency_is_bounded() -> None:
    node = SlowEcho()
    h = _harness(node, {"items": "reqs", "max_concurrent": 2})
    out = await h.run(Envelope("task", {"reqs": list("abcdef")}))
    assert isinstance(out, Done)
    assert node.peak <= 2, "bound violated: peak={}".format(node.peak)
    assert node.peak == 2, "items should actually run concurrently (peak={})".format(node.peak)


async def scenario_results_stay_in_item_order_despite_completion_order() -> None:
    node = SlowEcho(delays={"a": 0.03, "b": 0.001, "c": 0.02})  # a finishes LAST
    h = _harness(node, {"items": "reqs", "max_concurrent": 3})
    out = await h.run(Envelope("task", {"reqs": ["a", "b", "c"]}))
    assert isinstance(out, Done)
    assert [r["payload"]["upper"] for r in out.output.payload["results"]] == ["A", "B", "C"]


# --- failures --------------------------------------------------------------------------------

async def scenario_failed_item_fails_the_stage_naming_the_index() -> None:
    for mode in ("raise", "error", "verdict"):
        h = _harness(FailOn("b", mode), {"items": "reqs"})
        try:
            await h.run(Envelope("task", {"reqs": ["a", "b", "c"]}))
            raise AssertionError("a failed item ({}) must fail the stage".format(mode))
        except StageFailed as e:
            assert e.output is not None, mode
            assert e.output.payload["failed_items"] == [1], (mode, e.output.payload)
            # healthy results survive, pairs intact (compaction-safe provenance)
            assert [r["item_index"] for r in e.output.payload["results"]] == [0, 2], mode


async def scenario_min_success_passes_kofn_with_failed_items_marked() -> None:
    h = _harness(FailOn("b", "verdict"), {"items": "reqs"}, min_success=2)
    out = await h.run(Envelope("task", {"reqs": ["a", "b", "c"]}))
    assert isinstance(out, Done), out
    p = out.output.payload
    assert p["failed_items"] == [1], p
    assert [r["item_index"] for r in p["results"]] == [0, 2], p
    # and NOT enough successes still fails
    h2 = _harness(FailOn("b", "verdict"), {"items": "reqs"}, min_success=2)
    try:
        await h2.run(Envelope("task", {"reqs": ["a", "b"]}))
        raise AssertionError("min_success unmet must fail the stage")
    except StageFailed:
        pass


async def scenario_await_parks_the_whole_stage() -> None:
    h = _harness(FailOn("b", "await"), {"items": "reqs"})
    out = await h.run(Envelope("task", {"reqs": ["a", "b"]}))
    assert isinstance(out, Suspended), out


class TransientFailOn:
    """One item replies a transient-LOOKING error; counts every invocation."""

    def __init__(self, bad_item: str) -> None:
        self.bad = bad_item
        self.calls = 0

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        self.calls += 1
        item = input.payload.get("item")
        if item == self.bad:
            return input.reply_with(Kind.ERROR, {"error": "overloaded: 429 rate limit"})
        return input.reply_with(Kind.RESULT, {"upper": str(item).upper()})


async def scenario_transient_item_failure_does_not_rerun_the_swarm() -> None:
    # impl-eval MED: a foreach_error whose message embeds one item's 429 detail
    # tripped the TRANSIENT retry (error_retries=2 default) → the WHOLE swarm
    # re-ran, 15 calls for 5 items. A swarm is not pre-effect (the healthy items'
    # cost already happened), so transient re-runs must not apply — min_success
    # is the partial-failure tool.
    node = TransientFailOn("b")
    h = _harness(node, {"items": "reqs"})
    try:
        await h.run(Envelope("task", {"reqs": ["a", "b", "c", "d", "e"]}))
        raise AssertionError("the failed item must fail the stage")
    except StageFailed:
        pass
    assert node.calls == 5, "whole-swarm transient re-run detected: {} calls".format(node.calls)


# --- bad/edge input --------------------------------------------------------------------------

async def scenario_missing_or_non_list_items_fails_loud() -> None:
    for payload in ({}, {"reqs": "not-a-list"}, {"reqs": {"a": 1}}):
        h = _harness(Echo(), {"items": "reqs"})
        try:
            await h.run(Envelope("task", dict(payload)))
            raise AssertionError("bad items input must fail loud: {}".format(payload))
        except StageFailed as e:
            assert "reqs" in str(e), e


async def scenario_empty_list_is_a_pass_with_no_results() -> None:
    h = _harness(Echo(), {"items": "reqs"})
    out = await h.run(Envelope("task", {"reqs": []}))
    assert isinstance(out, Done), out
    assert out.output.payload["results"] == [] and out.output.payload["failed_items"] == []
    # min_success >= 1 turns the empty swarm into a failure
    h2 = _harness(Echo(), {"items": "reqs"}, min_success=1)
    try:
        await h2.run(Envelope("task", {"reqs": []}))
        raise AssertionError("empty swarm with min_success must fail")
    except StageFailed:
        pass


# --- hardening: coverage gaps from the impl-eval ----------------------------------------

async def scenario_foreach_inside_fork_branch() -> None:
    # FALSIFIES: ForkCoordinator._walk → _exec_stage → _run_stage reaches foreach
    # fine; the swarm runs inside the branch, results merge into the branch envelope,
    # and the fan-in receives the branch output carrying {results, failed_items}.
    # Would catch: foreach inside a fork raising, dropping results, or the fan-in
    # never clearing (and the run hanging).
    worker = Echo()
    seen: list = []

    class _Capture:
        async def invoke(self, inp: Envelope, config: NodeConfig) -> Envelope:
            seen.append(dict(inp.payload))
            return inp.reply_with(Kind.RESULT, dict(inp.payload))

    comms = InProcessComms()
    comms.register("role:worker", worker)
    comms.register("role:summary", _Capture())
    graph = Graph.of(
        Stage("spread", node="", fork=["scan"], then="summary"),
        Stage("scan", node="role:worker",
              foreach={"items": "reqs", "into": "item"}, then="join"),
        Stage("join", node="", fanin={"expect": ["scan"], "wait": "all"}, then=None),
        Stage("summary", node="role:summary", then=None),
    )
    out = await Harness(comms, graph).run(Envelope("task", {"reqs": ["x", "y", "z"]}))
    assert isinstance(out, Done), out
    assert len(seen) == 1, "summary must run exactly once: {}".format(seen)
    p = seen[0]
    assert p["failed_items"] == [], p
    assert [r["item_index"] for r in p["results"]] == [0, 1, 2], p["results"]
    assert [r["payload"]["upper"] for r in p["results"]] == ["X", "Y", "Z"], p["results"]
    assert len(worker.inputs) == 3, "all 3 items must have run inside the branch"


async def scenario_escalate_human_on_foreach_parks_with_merged_output() -> None:
    # FALSIFIES: escalate:"human" on a foreach stage parks with the MERGED output
    # (results + failed_items present and correct on the baton artifact), not an
    # empty or pre-merge envelope. Would catch: the harness discarding the merged
    # envelope before escalating, or a total-failure swarm not building it at all.
    class _AlwaysFail:
        async def invoke(self, inp: Envelope, config: NodeConfig) -> Envelope:
            raise RuntimeError("always fails")

    comms = InProcessComms()
    comms.register("role:worker", _AlwaysFail())
    graph = Graph.of(Stage(
        "swarm", node="role:worker",
        foreach={"items": "reqs"},
        max_attempts=1, escalate="human"))
    h = Harness(comms, graph)
    out = await h.run(Envelope("task", {"reqs": ["a", "b", "c"]}))
    assert isinstance(out, Suspended), out
    assert out.awaiting == "human:swarm", out.awaiting
    baton = await h.batons.load(out.baton_id)
    p = baton.pending.payload
    assert "results" in p and "failed_items" in p, p    # merge ran before escalation
    assert p["results"] == [], p                         # all items failed: no successes
    assert p["failed_items"] == [0, 1, 2], p             # all item indexes named
    esc = p.get("escalation")
    assert esc is not None and esc["stage"] == "swarm", p


async def scenario_escalate_human_partial_success_keeps_healthy_results() -> None:
    # Adversarial-eval MED fix: the all-fail case above cannot distinguish
    # "merge ran, no successes" from "results dropped" — results==[] is trivially
    # satisfied either way. Here 2 of 3 items SUCCEED but min_success=3 is unmet,
    # so the stage escalates WITH healthy results in hand: the parked artifact
    # must carry the successful items' pairs. Would catch: the escalate path
    # discarding or emptying successful results.
    comms = InProcessComms()
    comms.register("role:worker", FailOn("b", "verdict"))
    graph = Graph.of(Stage(
        "swarm", node="role:worker",
        foreach={"items": "reqs"}, min_success=3,
        max_attempts=1, escalate="human"))
    h = Harness(comms, graph)
    out = await h.run(Envelope("task", {"reqs": ["a", "b", "c"]}))
    assert isinstance(out, Suspended), out
    assert out.awaiting == "human:swarm", out.awaiting
    baton = await h.batons.load(out.baton_id)
    p = baton.pending.payload
    assert [r["item_index"] for r in p["results"]] == [0, 2], p    # successes SURVIVE the park
    assert [r["payload"]["upper"] for r in p["results"]] == ["A", "C"], p
    assert p["failed_items"] == [1], p


async def scenario_feedback_retry_foreach_items_intact_no_feedback_keys_per_item() -> None:
    # FALSIFIES: (a) per-item worker inputs must NOT carry feedback/priorAttempt
    # on a retry (ADR-0007 D3: REPLACE contract — per-item payload is exactly
    # {into, item_index, carry, sticky}, never a full inbound copy); (b) the items
    # list is re-read intact from the retry input; (c) feedback WAS actually
    # applied at the STAGE level (adversarial-eval MED fix: without this, an
    # engine with feedback=True unimplemented passes the leak checks trivially).
    # The foreach merge is inbound-payload ∪ {results, failed_items}, so on
    # attempt 2 the VALIDATOR's input must carry feedback/priorAttempt — that is
    # the observable proof _with_feedback ran, while workers stay clean.
    # Would catch: _item_env copying the whole inbound payload (leak), the retry
    # losing the items list, or stage.feedback silently not applied.
    worker = Echo()

    class _FirstFail:
        def __init__(self) -> None:
            self.inputs: list = []

        async def invoke(self, inp: Envelope, config: NodeConfig) -> Envelope:
            self.inputs.append(dict(inp.payload))
            if len(self.inputs) < 2:
                return Verdict.failed(
                    Failure("needs_retry", "fail first attempt", "retry")
                ).to_envelope(inp)
            return Verdict.passed().to_envelope(inp)

    checker = _FirstFail()
    comms = InProcessComms()
    comms.register("role:worker", worker)
    comms.register("role:check", checker)
    graph = Graph.of(Stage(
        "swarm", node="role:worker",
        foreach={"items": "reqs"},
        validators=["role:check"],
        max_attempts=2, feedback=True))
    out = await Harness(comms, graph).run(Envelope("task", {"reqs": ["a", "b"]}))
    assert isinstance(out, Done), out
    # 2 attempts × 2 items = 4 total worker invocations
    assert len(worker.inputs) == 4, (
        "expected 4 calls (2 attempts × 2 items), got {}".format(len(worker.inputs)))
    # feedback/priorAttempt must NOT appear in per-item inputs on ANY attempt
    for seen in worker.inputs:
        assert "feedback" not in seen, (
            "feedback leaked into per-item input (D3 violation): " + repr(seen))
        assert "priorAttempt" not in seen, (
            "priorAttempt leaked into per-item input (D3 violation): " + repr(seen))
    # Items list intact on retry: same items processed across both attempts
    items_seen = sorted(d.get("item") for d in worker.inputs)
    assert items_seen == ["a", "a", "b", "b"], (
        "items list changed on retry: " + repr(items_seen))
    # feedback WAS applied at the stage level: attempt 2's merged output (what
    # the validator sees) carries the feedback keys the retry input gained;
    # attempt 1's does not. Proves _with_feedback ran, not just didn't leak.
    assert len(checker.inputs) == 2, checker.inputs
    assert "feedback" not in checker.inputs[0], checker.inputs[0]
    assert "feedback" in checker.inputs[1] and "priorAttempt" in checker.inputs[1], (
        "stage.feedback not applied on retry (validator saw no feedback keys): "
        + repr(sorted(checker.inputs[1])))
    assert checker.inputs[1]["feedback"][0]["code"] == "needs_retry", checker.inputs[1]


async def scenario_await_inside_fork_branch_foreach_raises_runtime_error() -> None:
    # PINS behavior (ADR-0007 D3 + fork_coordinator._walk): when a foreach item
    # inside a fork branch returns Kind.AWAIT, the stage produces _Suspend,
    # _walk detects _Suspend and raises RuntimeError("gates inside a fork are
    # unsupported"). This test EXECUTES the path the impl-eval only read — if
    # reality diverges, the assertion messages name the discrepancy.
    # Would catch: _walk not checking _Suspend from foreach, _Suspend being
    # silently dropped, or a different exception type surfacing.
    comms = InProcessComms()
    comms.register("role:worker", FailOn("b", "await"))
    graph = Graph.of(
        Stage("spread", node="", fork=["scan"], then=None),
        Stage("scan", node="role:worker",
              foreach={"items": "reqs"}, then=None),
    )
    exc: BaseException = None  # type: ignore[assignment]
    try:
        await Harness(comms, graph).run(Envelope("task", {"reqs": ["a", "b", "c"]}))
        raise AssertionError(
            "AWAIT inside fork-branch foreach must raise, but run completed normally")
    except RuntimeError as e:
        exc = e
    except StageFailed as e:
        # ENGINE BUG? StageFailed instead of RuntimeError means the "gates inside
        # a fork unsupported" guard in ForkCoordinator._walk is NOT reached for
        # foreach-AWAIT — the _Suspend is being misrouted before _walk sees it.
        exc = e
    assert isinstance(exc, RuntimeError), (
        "expected RuntimeError (gates-inside-fork guard), "
        "got {}: {}".format(type(exc).__name__, exc))
    assert "gates inside a fork" in str(exc) or "unsupported" in str(exc), str(exc)


def main() -> None:
    for scen in (scenario_maps_items_in_order_as_index_payload_pairs,
                 scenario_per_item_input_is_replace_plus_carries_and_sticky,
                 scenario_per_item_corr_is_the_run_corr,
                 scenario_custom_into_key,
                 scenario_concurrency_is_bounded,
                 scenario_results_stay_in_item_order_despite_completion_order,
                 scenario_failed_item_fails_the_stage_naming_the_index,
                 scenario_min_success_passes_kofn_with_failed_items_marked,
                 scenario_await_parks_the_whole_stage,
                 scenario_transient_item_failure_does_not_rerun_the_swarm,
                 scenario_missing_or_non_list_items_fails_loud,
                 scenario_empty_list_is_a_pass_with_no_results,
                 scenario_foreach_inside_fork_branch,
                 scenario_escalate_human_on_foreach_parks_with_merged_output,
                 scenario_escalate_human_partial_success_keeps_healthy_results,
                 scenario_feedback_retry_foreach_items_intact_no_feedback_keys_per_item,
                 scenario_await_inside_fork_branch_foreach_raises_runtime_error):
        asyncio.run(scen())
    print("test_foreach: PASS (17 scenarios)")


if __name__ == "__main__":
    main()
