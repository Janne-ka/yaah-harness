"""Baton — the unit of ownership and the resume cursor for one run.

Used by: Harness (creates one per run, advances it through stages, parks it on
suspend, looks it up on resume, evicts it when the run ends).
Where: the resume cursor for an in-flight run, keyed by id in Harness._batons.
Why: exactly one holder at a time (no double-processing). It carries only what
resume needs — which stage to continue from and the run status — not a copy of
every stage's output (that was a leak with no reader; durable run state, when we
add it, belongs in the substrate, see docs/TODO.md "Durable baton + state store").
The Harness keeps a baton ONLY while it is resumable (suspended); terminal runs
are evicted, so _batons does not grow one entry per task forever.

Targets Python 3.9+.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..core import Envelope

# A baton nobody claims is swept after this long, in SECONDS. 72 hours — a run
# that parks at a human gate on Friday must still be resumable Monday morning (a
# shorter default swept the weekend's parked gates). Per-baton (each baton carries
# its own ttl); None = never. Override per deployment via root `baton_ttl`, whose
# value is passed straight through to `Baton.ttl` — SECONDS, not minutes.
# Since Level 2 this window has TWO meanings — see `is_expired`.
DEFAULT_BATON_TTL = 72 * 60 * 60.0


@dataclass
class Baton:
    id: str
    stage: Optional[str]
    status: str = "running"  # 'running' | 'suspended' | 'done'
    # When the run parked at a gate (clock reading), for TTL eviction of
    # suspended runs nobody ever resumes. None while running.
    parked_at: Optional[float] = None
    # How long this baton may stay parked before it's swept. The lifetime is the
    # baton's own property (not the harness's); None = live forever.
    ttl: Optional[float] = DEFAULT_BATON_TTL
    # Soft validator concerns gathered across the run — small dicts (not full
    # envelopes), surfaced on the final output and at human gates. Bounded, with
    # a real reader; survives suspend/resume so concerns aren't lost at a gate.
    concerns: List[dict] = field(default_factory=list)
    # The failed stage's last output, held while escalated to a human, so resume
    # can merge the human's decision onto the real artifact instead of replacing
    # it (early_review #18). One envelope, cleared on resume. None otherwise.
    pending: Optional[Envelope] = None
    # What this parked run is awaiting (e.g. 'human:data-audit') — set on suspend so
    # the mailbox view (BatonStore.list_suspended) can show the open question. None
    # while running.
    awaiting: Optional[str] = None
    # LEVEL 2 checkpoint durability (docs/durable-state.md §5). After each completed
    # stage the harness persists the baton with `stage` advanced to the NEXT stage,
    # `status="running"`, and the envelope that will feed it here — so a run KILLED
    # mid-flight is re-drivable from the in-flight stage (resume_running) instead of
    # lost. Set only while a running checkpoint exists; CLEARED when the baton parks
    # (a suspended gate is Level 1's own artifact, never also a running checkpoint).
    cursor_input: Optional[Envelope] = None
    # Wall-clock reading of the last checkpoint write — the running-checkpoint twin
    # of `parked_at`, so an abandoned (killed, never recovered) running checkpoint
    # is swept on the same ttl as an abandoned parked gate (is_expired below).
    checkpointed_at: Optional[float] = None

    def is_expired(self, now: float) -> bool:
        """True if this baton has outlived its ttl as of `now` (the harness supplies
        the clock reading; the policy lives here on the baton). Covers BOTH durable
        states the store holds: a SUSPENDED gate nobody resumed (past `parked_at`)
        and a RUNNING checkpoint nobody recovered after a crash (past
        `checkpointed_at`) — so a killed mid-run baton is reclaimed by the same
        sweep, not leaked forever.

        NOTE the DUAL MEANING one `ttl` now carries (v1, deliberate — one knob, not
        two). For a suspended gate it is a HUMAN patience window ("nobody answered
        in 72h"). For a running checkpoint the clock restarts at every completed
        stage, so it doubles as a bound on how long a SINGLE stage may be in flight:
        a stage running longer than `ttl` has its recovery record swept out from
        under it, and a crash after that point is unrecoverable. Deployments with a
        very long stage must set root `baton_ttl` (seconds) above that stage's
        worst-case wall-clock. Splitting the two windows is deferred until a real
        deployment needs different values."""
        if self.ttl is None:
            return False
        if self.status == "suspended" and self.parked_at is not None:
            return now - self.parked_at > self.ttl
        if self.status == "running" and self.checkpointed_at is not None:
            return now - self.checkpointed_at > self.ttl
        return False

    # -- serialization (for a durable BatonStore; see docs/durable-state.md) --
    # The baton must round-trip through bytes so a suspended run survives a restart
    # and can be resumed in another process. `pending` is an Envelope (already JSON
    # via to_dict/from_dict); everything else is scalars/small dicts.
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "stage": self.stage,
            "status": self.status,
            "parked_at": self.parked_at,
            "ttl": self.ttl,
            "concerns": list(self.concerns),
            "pending": self.pending.to_dict() if self.pending is not None else None,
            "awaiting": self.awaiting,
            "cursor_input": self.cursor_input.to_dict() if self.cursor_input is not None else None,
            "checkpointed_at": self.checkpointed_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Baton":
        pending = d.get("pending")
        cursor_input = d.get("cursor_input")
        return cls(
            id=d["id"],
            stage=d.get("stage"),
            status=d.get("status", "running"),
            parked_at=d.get("parked_at"),
            ttl=d.get("ttl", DEFAULT_BATON_TTL),
            concerns=list(d.get("concerns") or []),
            pending=Envelope.from_dict(pending) if pending is not None else None,
            awaiting=d.get("awaiting"),
            cursor_input=Envelope.from_dict(cursor_input) if cursor_input is not None else None,
            checkpointed_at=d.get("checkpointed_at"),
        )
