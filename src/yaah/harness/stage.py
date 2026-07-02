"""Stage — one node in the wiring graph (config, not code).

Used by: Graph (holds a map of these) and Harness (drives one per step).
Built by: yaah.build.build_graph from a pipeline config's `graph.stages`.
Where: the declarative description of a pipeline step.
Why: capture a step's wiring — which node, its validators, retry policy, human
escalation, next stage, fan-out roles, and conditional branch — as data.

Targets Python 3.9+.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Stage:
    name: str
    node: str  # role the worker is registered/served under
    id: Optional[str] = None  # configurable UNIQUE node id; addresses this gate's state
                              # (e.g. a clear) as <id>:<correlation_id>. Defaults to `name`.
    validators: List[str] = field(default_factory=list)  # roles, run in order (cheap first)
    max_attempts: int = 1
    error_retries: int = 2  # SEPARATE transient-fault budget (does NOT spend max_attempts):
                            # an infrastructural node/transport fault (provider overload/timeout,
                            # git index-lock) is retried-with-backoff this many times before it
                            # counts as a stage failure — so a blip can't fail a max_attempts:1 gate.
    feedback: bool = False  # feed the verdict back into the retry input
    escalate: Optional[str] = None  # 'human' → suspend when attempts run out
    then: Optional[str] = None  # next stage name, or None to finish
    fanout: Optional[List[str]] = None  # role BARRIER: run these ROLES in parallel on this one stage; merge to {results, roles}. Distinct from `fork` (branch chains) — explicit keys since the 2026-06-11 split.
    branch: Optional[Dict[str, Any]] = None  # conditional next: {on, routes:{val:stage}, default}
    fork: Optional[List[str]] = None  # FORK: spread the envelope to these successor STAGES, each runs independently
    fanin: Optional[Dict[str, Any]] = None  # JOIN: {expect, wait, timeout, on_timeout, reduce} — wait for branches, reduce, continue
    wait: Optional[Dict[str, Any]] = None  # FORK wait-for-clear: {timeout, on_timeout} — bound how long the fork waits for the fan-in
    clears: Optional[List[str]] = None  # node-ids this stage CLEARS on completion (publishes clear "<id>:<corr>") — reusable by any node, not just fan-in
    concerns_from: Optional[str] = None  # payload key holding a LIST of soft concerns this stage produced (e.g. a parsed sceptic report). On pass, the harness POPS the key and routes the items into baton.concerns — same channel as soft validators — so they surface at the NEXT human gate and in the final output without payload-threading through every stage in between (one missing carry would silently drop them).
    concerns_into: Optional[str] = None  # the INVERSE twin of concerns_from: before this stage runs, the harness INJECTS a copy of the concerns accumulated so far (baton.concerns) into the stage's input payload under this key — so a LATE stage (e.g. a report renderer) can show the run's soft-gate story, which otherwise only reaches the terminal Done output. A copy: downstream mutation can't corrupt engine state.
    clearable: bool = True   # EVERY node is clearable by default — a clear addressed to its id (or `*`) cancels its in-flight work; for a node with nothing in-flight to cancel the effect is just a no-op. Set False to OPT OUT — a committed side-effect / `once` node that can't be safely cancelled mid-flight (needs compensation, not cancel — see the clearable boundary).
    on_error: Optional[Any] = "clear"  # EVERY node has an error-clearing function; default = "clear":
                                       #   "clear"            → reversible (most nodes): drop this node's state (clear signal + store delete-by-node)
                                       #   {"compensate": T}  → side-effecting: run undo target T (call_target) then propagate
                                       #       + optional "on_compensate_fail": "error" (default, escalate loud if the undo itself fails) | "warn" (note in trace, tolerate)
                                       #   None               → opt out: fail straight through, no recovery
                                       # Run on TERMINAL failure, then the failure still propagates. clear stays dumb; this composes it.
