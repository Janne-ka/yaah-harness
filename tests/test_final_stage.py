"""Tests for the stage key `final: true` — skip the sticky re-fold on a
TERMINAL stage's output so its payload is the run's FINAL word.

Background: graph.sticky keys are re-folded (fill-if-missing) onto EVERY stage's
output, deliberately, so a payload-replacing node can't wipe the run frame. But a
terminal cleanup stage that projects a tidy final payload gets loop-state sticky
keys re-injected into the run's Done output. `final: true` opts that stage's
OUTPUT out of the re-fold. It is legal ONLY on a truly terminal stage (no
continuation key) — the run frame the dataflow lattice re-folds on every edge may
be dropped only by the last word.

Run: cd yaah && PYTHONPATH=src python3 tests/test_final_stage.py
"""
from __future__ import annotations

import asyncio
import os
import sys

from yaah import Done, Envelope, InProcessComms, Harness, NodeConfig
from yaah.build import build_graph
from yaah.validate import validate_pipeline


class Tidy:
    """A terminal projector (like verify-loop's emit_best): REPLACES the payload
    with just its own tidy keys, dropping everything upstream — including the
    sticky run-frame keys."""

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply_with(input.kind, {"best": input.payload.get("best_artifact", "")})


def _tidy_graph(final: bool):
    """One terminal stage that replaces the payload. `final` toggles the new key."""
    return build_graph({
        "start": "emit",
        "sticky": ["cycle", "loop_feedback", "best_artifact"],
        "stages": {
            "emit": {"node": "role:tidy", "then": None, "final": final},
        },
    })


async def scenario_final_terminal_drops_sticky() -> None:
    """FALSIFIER (fails today: sticky re-folds anyway). A `final: true` terminal
    stage's Done payload carries ONLY the stage's own output — the sticky keys
    it dropped are NOT re-injected."""
    comms = InProcessComms()
    comms.register("role:tidy", Tidy())
    out = await Harness(comms, _tidy_graph(final=True)).run(
        Envelope("task", {"cycle": 3, "loop_feedback": "vague", "best_artifact": "poem"}))
    assert isinstance(out, Done), out
    assert out.output.payload == {"best": "poem"}, out.output.payload
    assert "cycle" not in out.output.payload, out.output.payload
    assert "loop_feedback" not in out.output.payload, out.output.payload


async def scenario_nonfinal_terminal_keeps_sticky() -> None:
    """CONTROL: the same terminal stage WITHOUT `final` re-folds sticky as
    before — cycle/loop_feedback reappear in the Done output (today's behavior,
    unchanged)."""
    comms = InProcessComms()
    comms.register("role:tidy", Tidy())
    out = await Harness(comms, _tidy_graph(final=False)).run(
        Envelope("task", {"cycle": 3, "loop_feedback": "vague", "best_artifact": "poem"}))
    assert isinstance(out, Done), out
    assert out.output.payload["cycle"] == 3, out.output.payload
    assert out.output.payload["loop_feedback"] == "vague", out.output.payload
    assert out.output.payload["best"] == "poem", out.output.payload


async def scenario_final_only_affects_final_stage() -> None:
    """A `final` stage does not disturb an EARLIER stage's sticky re-fold: the
    first (non-final) stage still gets sticky folded; only the last word drops
    it."""
    comms = InProcessComms()
    comms.register("role:tidy", Tidy())
    graph = build_graph({
        "start": "mid",
        "sticky": ["cycle", "best_artifact"],
        "stages": {
            # `mid` replaces the payload too, but is NOT final → sticky re-folds,
            # so `emit` still sees cycle/best_artifact on its input.
            "mid": {"node": "role:tidy", "then": "emit"},
            "emit": {"node": "role:tidy", "then": None, "final": True},
        },
    })
    out = await Harness(comms, graph).run(
        Envelope("task", {"cycle": 2, "best_artifact": "poem"}))
    assert isinstance(out, Done), out
    # emit projected best_artifact (which survived to its input via the mid re-fold)
    assert out.output.payload == {"best": "poem"}, out.output.payload


def _validate_raises(stages, nodes=None, start=None):
    """Return the ValueError message from validate_pipeline, or None if it passed."""
    cfg = {
        "nodes": nodes or {
            "role:n": {"type": "transform", "call": "envelope", "target": "fn:x:y"}},
        "graph": {"start": start or next(iter(stages)), "stages": stages},
    }
    try:
        validate_pipeline(cfg)
        return None
    except ValueError as e:
        return str(e)


def rejects_final_with_then() -> None:
    msg = _validate_raises({
        "a": {"node": "role:n", "then": "b", "final": True},
        "b": {"node": "role:n", "then": None},
    })
    assert msg and "final" in msg and "'a'" in msg, msg


def rejects_final_with_branch() -> None:
    msg = _validate_raises({
        "a": {"node": "role:n", "final": True,
              "branch": {"on": "x", "routes": {"y": "b"}, "default": "b"}},
        "b": {"node": "role:n", "then": None},
    })
    assert msg and "final" in msg and "'a'" in msg, msg


def rejects_final_with_fork() -> None:
    msg = _validate_raises({
        "a": {"fork": ["b"], "final": True},
        "b": {"node": "role:n", "then": None},
    })
    assert msg and "final" in msg and "'a'" in msg, msg


def rejects_final_with_fanout() -> None:
    msg = _validate_raises({
        "a": {"fanout": ["role:n"], "final": True},
    })
    assert msg and "final" in msg and "'a'" in msg, msg


def rejects_final_with_fanin() -> None:
    msg = _validate_raises({
        "j": {"fanin": {"expect": []}, "final": True},
    })
    assert msg and "final" in msg and "'j'" in msg, msg


def rejects_final_with_foreach() -> None:
    msg = _validate_raises({
        "a": {"node": "role:n", "foreach": {"items": "xs"}, "final": True},
    })
    assert msg and "final" in msg and "'a'" in msg, msg


def rejects_non_bool_final() -> None:
    msg = _validate_raises({
        "a": {"node": "role:n", "then": None, "final": "yes"},
    })
    assert msg and "final" in msg, msg


def rejects_final_inside_fork_scope() -> None:
    """The adversarial-eval defect: a `final` stage that is TERMINAL (no
    continuation) but reached through fork/fan-in machinery is walked by the
    ForkCoordinator, whose fold sites are unconditional — so `final` would
    silently do nothing. Reject it loud instead of a walker-dependent no-op.

    Shape: fork F -> branch `a` -> fan-in `j` (then `emit`); `emit` is a terminal
    `final: true` stage reached only via the fan-in's `then` chain."""
    msg = _validate_raises({
        "F": {"fork": ["a"], "then": None},
        "a": {"node": "role:n", "then": "j"},
        "j": {"fanin": {"expect": ["a"]}, "then": "emit"},
        "emit": {"node": "role:n", "then": None, "final": True},
    }, start="F")
    assert msg and "final" in msg and "'emit'" in msg and "fork" in msg, msg


def rejects_final_on_fork_branch_terminal() -> None:
    """A fork BRANCH terminal stage (walked by the coordinator, output discarded)
    with `final` is likewise a no-op — reject it."""
    msg = _validate_raises({
        "F": {"fork": ["a"], "then": None},
        "a": {"node": "role:n", "then": None, "final": True},
    }, start="F")
    assert msg and "final" in msg and "'a'" in msg, msg


def accepts_final_after_fork_on_fork_then() -> None:
    """A `final` cleanup on the FORK stage's own `then` (the linear continuation
    after the fork resolves) IS walked by _drive → honored → allowed."""
    msg = _validate_raises({
        "F": {"fork": ["a"], "then": "emit", "wait": {"timeout": 1}},
        "a": {"node": "role:n", "then": "j"},
        "j": {"fanin": {"expect": ["a"]}},
        "emit": {"node": "role:n", "then": None, "final": True},
    }, start="F")
    assert msg is None, msg


def accepts_final_on_terminal_stage() -> None:
    """The whole point: a terminal stage (no continuation) may carry final."""
    msg = _validate_raises({
        "a": {"node": "role:n", "then": None, "final": True},
    })
    assert msg is None, msg


def accepts_final_on_one_stage_start() -> None:
    """final on the START stage of a one-stage pipeline (start IS terminal)."""
    msg = _validate_raises({
        "only": {"node": "role:n", "final": True},
    })
    assert msg is None, msg


def accepts_final_false_with_then() -> None:
    """final: false is a no-op — it must NOT trip the terminal-only check."""
    msg = _validate_raises({
        "a": {"node": "role:n", "then": "b", "final": False},
        "b": {"node": "role:n", "then": None},
    })
    assert msg is None, msg


def build_graph_threads_final() -> None:
    g = build_graph({
        "start": "a",
        "stages": {"a": {"node": "role:n", "final": True}},
    })
    assert g.stages["a"].final is True
    # default is False when omitted
    g2 = build_graph({"start": "a", "stages": {"a": {"node": "role:n"}}})
    assert g2.stages["a"].final is False


async def scenario_verify_loop_example_is_tidy() -> None:
    """Integration: the real verify-loop example, run offline through the actual
    runtime, ends with a Done payload that has NO loop-state sticky keys
    (cycle/loop_feedback) — the whole reason `final` exists."""
    from yaah.runtime import run_root
    from yaah.runtime_factories import _read_json

    here = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "examples", "verify-loop"))
    if here not in sys.path:  # so `fn:transforms:tally` imports, as the CLI arranges
        sys.path.insert(0, here)
    root = _read_json(os.path.join(here, "verify-loop.local.json"))
    out = await run_root(root, here)
    assert isinstance(out, Done), out
    p = out.output.payload
    assert "cycle" not in p, p
    assert "loop_feedback" not in p, p
    assert p.get("best_artifact"), p
    assert "total_cycles" in p, p


async def main() -> None:
    await scenario_final_terminal_drops_sticky()
    await scenario_nonfinal_terminal_keeps_sticky()
    await scenario_final_only_affects_final_stage()
    rejects_final_with_then()
    rejects_final_with_branch()
    rejects_final_with_fork()
    rejects_final_with_fanout()
    rejects_final_with_fanin()
    rejects_final_with_foreach()
    rejects_non_bool_final()
    rejects_final_inside_fork_scope()
    rejects_final_on_fork_branch_terminal()
    accepts_final_after_fork_on_fork_then()
    accepts_final_on_terminal_stage()
    accepts_final_on_one_stage_start()
    accepts_final_false_with_then()
    build_graph_threads_final()
    await scenario_verify_loop_example_is_tidy()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
