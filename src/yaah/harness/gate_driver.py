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
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple, Union

from ..core import Envelope, Kind
from .done import Done
from .suspended import Suspended

# A decider turns a parked run into the human/external decision to resume it with.
# May be sync or async. Returns the response Envelope handed to Harness.resume(),
# or None to signal "no decision for this gate" — the driver then leaves the run
# parked and returns the Suspended outcome (a walk-away is a resumable baton, not
# a crash).
Decider = Callable[[Suspended], Union[Optional[Envelope], Awaitable[Optional[Envelope]]]]


async def drive(harness: Any, task: Envelope, decide: Decider, *,
                max_gates: int = 1000, **run_kwargs: Any) -> Union[Done, Suspended]:
    """Run `task` through `harness`, resuming at each gate with a decision from
    `decide`, until a Done — or until `decide` returns None for a gate, at which
    point the run is left PARKED and that Suspended is returned (the caller can
    resume it later). `run_kwargs` (e.g. ttl=) pass through to the initial run().
    `max_gates` bounds the resume loop so a mis-wired gate that re-suspends
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
        if decision is None:
            return outcome  # no decision for this gate: leave the run parked
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
    `decisions` is a map of gate answers keyed by a gate's awaiting tag; an
    AUTHORED convenience gate matches loosely (tried whole, then the parts either
    side of ':'), but a FAULT park — the escalate lane, tagged 'human:<stage>' —
    matches an EXACT key only (see decide). `interactive` falls back to stdin.
    Used by: runtime.run_root.

    Lives here (next to drive()) because resolving WHERE a decision comes from —
    a config map or stdin — is gate-driver policy, not runtime assembly."""
    decisions = root.get("decisions")
    interactive = bool(root.get("interactive", False))
    if not decisions and not interactive:
        return None

    async def decide(suspended: Suspended) -> Optional[Envelope]:
        if decisions:
            awaiting = suspended.awaiting
            # A FAULT park (the escalate lane) is tagged 'human:<stage>'. It must
            # match an EXACT decisions key — never the loose split-on-':' fallback,
            # or a parked FAILURE 'human:merge' would suffix-match decisions['merge']
            # and silently auto-approve the fault (masked-failure class, M13).
            # Authored convenience gates (no 'human:' prefix) keep loose matching.
            candidates: "Tuple[str, ...]"
            if awaiting.startswith("human:"):
                candidates = (awaiting,)
            else:
                candidates = (awaiting, awaiting.split(":", 1)[-1], awaiting.split(":", 1)[0])
            for key in candidates:
                if key in decisions:
                    return Envelope(Kind.RESUME, dict(decisions[key]))
        if interactive:
            return _stdin_decision(suspended)
        # No decision for this gate: leave the run PARKED (drive returns the
        # Suspended). A walk-away run ends as a resumable baton, not a crash.
        return None

    return decide
