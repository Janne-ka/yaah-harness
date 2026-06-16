"""InMemorySubscription — the in-memory subscription handle.

Used by: InProcessComms.subscribe / LocalBus.subscribe (returned to callers
who'll later call .cancel()).
Where: in-process transports only — these don't talk to a broker so the
subscription state is a position in a shared handler list.
Why: implements the `Subscription` Protocol (yaah.comms.Subscription) — the
contract is just `.cancel()`. Renamed from `Subscription` after the Protocol
was promoted to the Comms port so the two don't collide; this is the
in-memory IMPLEMENTATION, the Protocol is the cross-transport CONTRACT
(NATS's adapter wraps its native sub in `_NatsSubscription` to satisfy the
same Protocol).

Targets Python 3.9+.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .comms import Handler


@dataclass
class InMemorySubscription:
    topic: str
    _handlers: List[Handler]
    _handler: Handler

    def cancel(self) -> None:
        try:
            self._handlers.remove(self._handler)
        except ValueError:
            pass
