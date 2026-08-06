"""LeaseState — is the process that owns this running checkpoint still alive?

Used by: Harness.resume_running (refuses to re-drive a checkpoint whose owner is
still running) and the `yaah list` surface (labels each RUNNING line, and prints
the resume-run hint only when the lease is not live). ONE home for the tiered
probe so the recovery decision and the operator's label can never disagree.
Where: yaah.harness — the liveness half of docs/durable-state.md §10 ("single-owner
baton"); the CAS half is BatonStore.claim.
Why: before this, `list_running` could not tell a CRASHED run from one still live
in another process, so every recovery was the CALLER asserting death. The owner
stamp (`<host>/<pid>/<nonce>`, refreshed at every checkpoint) makes the common
case — same host, dead pid — decidable by the engine.

The three tiers, weakest evidence last:
  1. NO owner — a pre-upgrade record. Allowed, with a note: the engine has no
     evidence either way and refusing would strand records written before the
     lease existed.
  2. SAME host — `os.kill(pid, 0)` is real evidence. Dead pid => recover
     immediately. Live pid => refuse (that process is still driving the run).
     "Same host" means the same NAME, which is `socket.gethostname()` unless root
     `lease_host` overrides it. In a CONTAINER that name is the pod id and changes
     on every restart, so a deployment that does not set `lease_host` never reaches
     this tier for its own runs — everything looks foreign and falls to tier 3.
  3. FOREIGN host — no probe is possible, so fall back to the lease AGE against
     `lease_horizon`: older than the horizon => presumed dead (allow, warn),
     within it => refuse.

v1 non-proofs, deliberate and documented rather than defended against:
  - CLOCK SKEW between hosts distorts tier-3 ages; the horizon is a coarse
    fallback, not a consensus protocol.
  - A SIGSTOP'd process answers `kill(pid, 0)` and so refuses FOREVER — the
    escape is `--force`.
  - PID REUSE can report a dead owner as live (false refuse, the safe direction)
    on a host that has cycled through its pid space.
  - NFS/`flock` weakness means the CAS claim behind this check is advisory on
    some shared filesystems (see BatonStore.claim).
  - The CAS claim fences RECOVERY, not the INCUMBENT: `Harness._checkpoint` saves
    unconditionally, so an owner wrongly presumed dead (tier 3 past the horizon, or
    a `--force`) re-takes the record at its next stage boundary rather than being
    stopped. Closing it needs a per-checkpoint CAS (durable-state.md §11 Phase B).
None of these can be closed without a real consensus store; all of them fail
toward "refuse" or "one loud --force", never toward a silent double-drive — except
the incumbent case, which is why that one names its fix.

Targets Python 3.9+.
"""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from typing import Optional

# Fallback presumed-dead window for a FOREIGN host's lease, SECONDS. One hour:
# long enough that an ordinary stage running on another host is not declared dead
# mid-flight, short enough that an operator is not blocked for a shift after a
# host dies. Override per deployment via root `lease_horizon`.
DEFAULT_LEASE_HORIZON = 3600.0

# The label vocabulary, shared by the refusal messages and `yaah list`:
#   none    — no owner stamped (pre-upgrade record)
#   live    — the owning process answered a liveness probe
#   stale   — presumed dead (dead pid on this host, or past the horizon elsewhere)
#   foreign — owned by another host, lease still within the horizon
NONE, LIVE, STALE, FOREIGN = "none", "live", "stale", "foreign"


def _this_host() -> str:
    return socket.gethostname()


def mint_owner(host: Optional[str] = None) -> str:
    """A fresh owner id for one Harness: `<host>/<pid>/<nonce8>`. The nonce
    distinguishes two harnesses in the SAME process (tests, an embedding app
    driving two pipelines) — pid alone would make them indistinguishable.

    `host` is the root `lease_host` override, threaded down from Harness. Absent =
    `gethostname()`; a CONTAINER wants a stable per-kernel name instead, because
    `gethostname()` there is the pod id and changes on every restart (see
    docs/durable-state.md §5)."""
    # `uuid` is imported HERE, not at module scope: this module is on the `yaah list`
    # import path, which runs for every operator invocation, while `mint_owner` is
    # called once per Harness. `uuid` pulls `ctypes` on some platforms — a real cost
    # for a surface that mostly only reads and LABELS leases.
    import uuid
    return "{}/{}/{}".format(host or _this_host(), os.getpid(), uuid.uuid4().hex[:8])


def _age_label(age: Optional[float]) -> str:
    if age is None:
        return "?"
    m = int(age // 60)
    return "{}h{:02d}m".format(m // 60, m % 60)


@dataclass
class LeaseState:
    state: str                    # NONE | LIVE | STALE | FOREIGN
    owner: Optional[str]
    age: Optional[float]          # seconds since the lease was stamped
    detail: str                   # one operator-readable sentence

    @property
    def recoverable(self) -> bool:
        """True when re-driving this checkpoint is allowed WITHOUT `--force`."""
        return self.state in (NONE, STALE)

    def label(self) -> str:
        """The compact `yaah list` token: `live`, `none`, `stale(2h14m)`,
        `foreign(2h14m)`."""
        if self.state in (NONE, LIVE):
            return self.state
        return "{}({})".format(self.state, _age_label(self.age))

    @classmethod
    def of(cls, baton: object, now: float,
           horizon: float = DEFAULT_LEASE_HORIZON,
           host: Optional[str] = None,
           alive: Optional[object] = None) -> "LeaseState":
        """Probe the lease on `baton` as of wall-clock `now`. `host` (the root
        `lease_host` override; None = this host's name) and `alive` (a `pid -> bool`
        callable) are injectable so a test can drive every tier without spawning real
        processes; production defaults are `gethostname()` and `os.kill(pid, 0)`.

        `baton` is typed `object` and read with `getattr`, NOT typed as `Baton`, and
        that is the point: the dependency runs ONE WAY. `Baton` is the durable record;
        this is a policy read over two of its fields. Importing `Baton` here would
        make the liveness policy a peer of the record and invite the reverse import
        (a `Baton.lease` property), which is how a state module ends up owning
        decisions. The `getattr` defaults also make a pre-upgrade record — literally a
        Baton without these attributes — fall into the NONE tier instead of raising."""
        owner = getattr(baton, "owner", None)
        leased_at = getattr(baton, "leased_at", None)
        age = (now - leased_at) if leased_at is not None else None
        if not owner:
            return cls(NONE, None, None,
                       "unleased (pre-upgrade) record — no owner was ever stamped, "
                       "so the engine has no liveness evidence either way")
        owner_host, _, rest = owner.partition("/")
        pid_text = rest.partition("/")[0]
        if owner_host == (host or _this_host()) and pid_text.isdigit():
            if (alive or _pid_alive)(int(pid_text)):
                return cls(LIVE, owner, age,
                           "process {} on this host ({}) is still alive and driving "
                           "this run".format(pid_text, owner_host))
            return cls(STALE, owner, age,
                       "process {} on this host ({}) is gone".format(pid_text, owner_host))
        if age is not None and age > horizon:
            return cls(STALE, owner, age,
                       "owned by {} — another host, so liveness cannot be probed; the "
                       "lease is {} old, past the {:.0f}s lease_horizon, so its process "
                       "is presumed dead".format(owner, _age_label(age), horizon))
        return cls(FOREIGN, owner, age,
                   "owned by {} — another host, so liveness cannot be probed; the lease "
                   "is {} old, within the {:.0f}s lease_horizon".format(
                       owner, _age_label(age), horizon))


def _pid_alive(pid: int) -> bool:
    """`kill(pid, 0)` liveness probe. EPERM means the pid EXISTS but belongs to
    another user — alive, not absent; only ESRCH ("no such process") is death."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True     # unknown errno: assume alive, i.e. refuse (the safe direction)
    return True
