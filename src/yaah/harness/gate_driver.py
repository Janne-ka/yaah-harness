"""drive — run a gated pipeline to completion, collecting decisions at each gate.

Used by: the runtime (yaah.runtime) and apps that want a gated pipeline to finish
in one call instead of stopping at the first suspend; the test suite.
Where: policy ON TOP of the line — like build(), not a Harness method. The
Harness owns run()/resume()/baton lifecycle; this just loops them.
Why: Harness.run() returns at the FIRST gate (a Suspended), because the line is
ignorant of where a human decision comes from. This driver supplies that loop:
run -> while Suspended: ask the injected `decide` for a decision -> resume, until
Done. Keeping `decide` injected (sync or async, Suspended -> Envelope) keeps the
decision source — a config map, stdin, a UI node's mailbox — out of the harness.

Targets Python 3.9+.
"""
from __future__ import annotations

import inspect
import json
import sys
from typing import Any, Awaitable, Callable, Dict, Optional, Union

from ..core import Envelope, Kind
from .done import Done
from .suspended import Suspended

# A decider turns a parked run into the human/external decision to resume it with.
# May be sync or async; returns the response Envelope handed to Harness.resume().
Decider = Callable[[Suspended], Union[Envelope, Awaitable[Envelope]]]


async def drive(harness: Any, task: Envelope, decide: Decider, *,
                max_gates: int = 1000, **run_kwargs: Any) -> Done:
    """Run `task` through `harness` to a Done, resuming at each gate with a
    decision from `decide`. `run_kwargs` (e.g. ttl=) pass through to the initial
    run(). `max_gates` bounds the resume loop so a mis-wired gate that re-suspends
    forever can't spin — it raises rather than hang."""
    outcome = await harness.run(task, **run_kwargs)
    gates = 0
    while isinstance(outcome, Suspended):
        gates += 1
        if gates > max_gates:
            raise RuntimeError(
                "gate driver exceeded {} gates (baton={!r}, awaiting={!r}) — "
                "a gate likely re-suspends without progressing".format(
                    max_gates, outcome.baton_id, outcome.awaiting))
        decision = decide(outcome)
        if inspect.isawaitable(decision):
            decision = await decision
        outcome = await harness.resume(outcome.baton_id, decision)
    return outcome  # Done


def _stdin_decision(suspended: Suspended) -> Envelope:
    """Prompt the operator at a gate and read one line of JSON (or bare text) as
    the decision. Used by build_decider's interactive fallback — the simplest
    human gate; a UI node + mailbox is the richer, distributed version (TODO)."""
    print("\n[GATE] awaiting: {}".format(suspended.awaiting), flush=True)
    if getattr(suspended, "ask", ""):
        print("  question: {}".format(suspended.ask), flush=True)
    if suspended.concerns:
        print("  concerns: {}".format(json.dumps(suspended.concerns, indent=2)), flush=True)
    line = sys.stdin.readline()
    try:
        payload = json.loads(line) if line.strip() else {}
    except json.JSONDecodeError:
        payload = {"text": line.strip()}
    return Envelope(Kind.RESUME, payload)


def build_decider(root: Dict[str, Any]) -> Optional[Decider]:
    """Build the gate-driver's decider from the root config, or None if the run
    shouldn't auto-drive gates (preserving the run-once-then-stop default).
    `decisions` is a map of gate answers keyed by a gate's awaiting tag (tried
    whole, then the parts either side of ':', so 'human:data-audit' matches
    'data-audit'); `interactive` falls back to stdin. Used by: runtime.run_root.

    Lives here (next to drive()) because resolving WHERE a decision comes from —
    a config map or stdin — is gate-driver policy, not runtime assembly."""
    decisions = root.get("decisions")
    interactive = bool(root.get("interactive", False))
    if not decisions and not interactive:
        return None

    async def decide(suspended: Suspended) -> Envelope:
        if decisions:
            awaiting = suspended.awaiting
            for key in (awaiting, awaiting.split(":", 1)[-1], awaiting.split(":", 1)[0]):
                if key in decisions:
                    return Envelope(Kind.RESUME, dict(decisions[key]))
        if interactive:
            return _stdin_decision(suspended)
        raise RuntimeError(
            "run parked at gate {!r} but no matching decision in `decisions` "
            "(concerns={})".format(suspended.awaiting, suspended.concerns))

    return decide
