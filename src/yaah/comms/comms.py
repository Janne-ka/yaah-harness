"""Comms — the only interface the harness calls to move envelopes.

Used by: the harness (request/publish) and apps (subscribe). Implemented by:
InProcessComms, LocalBus, NatsComms.
Where: the boundary between the harness and any transport.
Why: three modes over one interface (request=call, publish=event,
subscribe) so the transport is swappable without touching the harness.
Handover is a pattern built on request, not a method here.

Targets Python 3.9+.
"""
from __future__ import annotations

from abc import abstractmethod
from typing import Awaitable, Callable, Protocol, runtime_checkable

from ..core import Envelope

# A subscriber handler for event-mode (publish) delivery.
Handler = Callable[[Envelope], Awaitable[None]]


@runtime_checkable
class Subscription(Protocol):
    """A handle returned by Comms.subscribe — the contract is `.cancel()`,
    a sync no-arg no-return idempotent teardown. Promoted to the Comms port
    (assessment cluster 1/2 CRITICAL #1): previously subscribe was typed as
    `-> object` and the harness blindly called `.cancel()` — the NATS adapter
    returned the raw `nats.aio.subscription.Subscription` (no `.cancel()`,
    only async `.unsubscribe()`), so every clearable stage and fork over NATS
    crashed in a `finally:`. With this Protocol, the contract is explicit and
    every adapter's return type is bound to it; a future transport that doesn't
    expose `.cancel()` fails to typecheck rather than at runtime."""

    @abstractmethod
    def cancel(self) -> None:
        ...


@runtime_checkable
class Comms(Protocol):
    @abstractmethod
    async def request(self, target: str, envelope: Envelope) -> Envelope:
        ...

    @abstractmethod
    async def publish(self, topic: str, envelope: Envelope) -> None:
        ...

    @abstractmethod
    async def subscribe(self, topic: str, handler: Handler) -> Subscription:
        ...
