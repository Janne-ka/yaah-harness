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
    # The RUNNING-checkpoint sweep window, SECONDS — the second half of the split
    # `ttl` (root `checkpoint_ttl`). None = inherit `ttl`, which is the pre-split
    # behaviour and the ONLY safe default: an engine-side number here would
    # silently SHORTEN an existing deployment's recovery window on upgrade.
    checkpoint_ttl: Optional[float] = None
    # LIVENESS LEASE (docs/durable-state.md §10). Who is driving this run right now:
    # "<host>/<pid>/<nonce8>", stamped by Harness on every checkpoint write. A
    # recovery caller uses it to tell a CRASHED run from one still live in another
    # process instead of asserting death (LeaseState). None on a parked gate (no
    # owning process) and on a pre-upgrade record.
    owner: Optional[str] = None
    # Wall clock of the last owner stamp — the lease's age. Comparable across
    # processes (same reason `parked_at` is wall, not monotonic). Nulled on park.
    leased_at: Optional[float] = None
    # The WIRING fingerprint of the graph that PRODUCED this cursor
    # (wiring_fingerprint): topology only — stages, targets, routes — never
    # prompts/models/timeouts. A recovery re-drives `stage` against the CURRENT graph,
    # so a topology edit between the kill and the recovery would resume onto the wrong
    # stage; the stamp is what makes that refusable. None on a pre-upgrade record
    # (checks skip).
    # NOT mint provenance: it is stamped at mint and RE-STAMPED by every checkpoint
    # (Harness._checkpoint), so after an `--allow-rewiring` recovery it names the
    # graph that actually ran. Otherwise the flag would be needed once per crash for
    # the rest of the run, each time asserting compatibility with a graph nobody was
    # driving any more.
    wiring: Optional[str] = None

    def is_expired(self, now: float) -> bool:
        """True if this baton has outlived its sweep window as of `now` (the harness
        supplies the clock reading; the policy lives here on the baton). The two
        durable states the store holds have SEPARATE windows, because they answer
        different questions:

          - a SUSPENDED gate is swept `ttl` after `parked_at` — a HUMAN patience
            window ("nobody answered in 72h");
          - a RUNNING checkpoint is swept `checkpoint_ttl` after `checkpointed_at`.
            That clock restarts at every completed stage, so this window is really
            a bound on how long ONE stage may be in flight: a stage running longer
            than it has its recovery record swept out from under it, and a crash
            after that point is unrecoverable.

        `checkpoint_ttl` None INHERITS `ttl` — the pre-split behaviour, kept as the
        default so an upgrade cannot silently shorten anybody's recovery window.
        Set root `checkpoint_ttl` (seconds) above your slowest stage's worst-case
        wall-clock; the human window stays whatever `baton_ttl` says."""
        if self.status == "suspended" and self.parked_at is not None:
            return self.ttl is not None and now - self.parked_at > self.ttl
        if self.status == "running" and self.checkpointed_at is not None:
            window = self.checkpoint_ttl if self.checkpoint_ttl is not None else self.ttl
            return window is not None and now - self.checkpointed_at > window
        return False

    # -- serialization (for a durable BatonStore; see docs/durable-state.md) --
    # The baton must round-trip through bytes so a suspended run survives a restart
    # and can be resumed in another process. `pending` is an Envelope (already JSON
    # via to_dict/from_dict); everything else is scalars/small dicts.
    # EVERY optional field reads with `.get(...)` defaulting to None, so a record
    # written by an OLDER engine (no owner/leased_at/wiring/checkpoint_ttl) still
    # loads — an upgrade must never strand the parked gates already in the store.
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
            "checkpoint_ttl": self.checkpoint_ttl,
            "owner": self.owner,
            "leased_at": self.leased_at,
            "wiring": self.wiring,
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
            checkpoint_ttl=d.get("checkpoint_ttl"),
            owner=d.get("owner"),
            leased_at=d.get("leased_at"),
            wiring=d.get("wiring"),
        )
