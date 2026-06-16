"""ClearBus — the clear-signal sub-system: address scheme, matching, publication.

Used by: Harness (a stage's `clears`, the agent-clear race in `_run_clearable`,
the `clear()` graceful-reset broadcast) and ForkCoordinator (the fork's
wait-for-clear subscription, the fan-in's clear publication) — BOTH hold one.
Where: constructed by Harness and handed to the ForkCoordinator; wraps the Comms
handle for the `clear` topic.
Why: clear signals are their own concept — the address scheme
(`<node-id>:<correlation_id>`), the escalating match scopes, and sender-agnostic
delivery — but the logic used to live as Harness privates that the fork reached
into (review 2026-06-11, cluster 1: the homeless clear bus was the coupling's
cause). One named home; neither owner borrows the other's internals.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import List, Optional

from ..core import Envelope, Kind


class ClearBus:
    def __init__(self, comms: object, topic: str = "clear") -> None:
        self._comms = comms
        self._topic = topic

    @staticmethod
    def matches(cid: Optional[str], instance: str, node_id: str) -> bool:
        """Does a clear's `clear_id` address this gate/node? Three escalating scopes:
        the exact instance (`<node>:<corr>`); the node regardless of run (`<node>` —
        an error/blanket clear); or everything (`*` — flush). One matcher, shared by
        the fork's wait-for-clear and the agent-clear (cancel in-flight).

        SECURITY: the bare-node and `*` scopes CROSS RUN BOUNDARIES — anything that
        can publish to the `clear` topic with `clear_id="*"` cancels every in-flight
        clearable stage on this harness, in every concurrent run. That's the design
        for `Harness.clear()` (the graceful-reset admin op), but it means BROKER-LEVEL
        ACL is the security boundary in distributed deployments: only privileged
        publishers must be able to publish `clear`. See docs/durable-state.md / NATS
        subject permissions. In-process / single-tenant runs are unaffected (only
        the harness publishes)."""
        return cid == instance or cid == node_id or cid == "*"

    async def subscribe(self, handler, topic: Optional[str] = None):
        """Listen for clears (returns the transport's cancellable subscription).
        `topic` overrides the default for a fork's configured `clear_topic`."""
        return await self._comms.subscribe(topic or self._topic, handler)

    async def publish_clears(self, targets: List[str], corr: str, payload: dict,
                             topic: Optional[str] = None) -> None:
        """Publish a clear for each target gate node-id (address `<id>:<corr>`). The
        reusable 'this node clears these gates on completion' capability — used by the
        fan-in (it names the gate(s) it clears) and available to ANY stage via `clears`."""
        for t in targets or []:
            await self._comms.publish(topic or self._topic, Envelope(
                Kind.RESULT, dict(payload),
                {"correlation_id": corr, "clear_id": "{}:{}".format(t, corr)}))

    async def publish_clear(self, clear_id: str, corr: str, payload: dict,
                            topic: Optional[str] = None) -> None:
        """Publish one clear to an EXPLICIT address — the fan-in echoing its fork's
        own `clear_id` back (the automatic fork->fan-in pair)."""
        await self._comms.publish(topic or self._topic, Envelope(
            Kind.RESULT, dict(payload),
            {"correlation_id": corr, "clear_id": clear_id}))

    async def broadcast(self) -> None:
        """The `*` clear — every in-flight clearable node cancels, every waiting
        fork/gate releases. The graceful-reset half of `Harness.clear()`."""
        await self._comms.publish(self._topic, Envelope(Kind.RESULT, {}, {"clear_id": "*"}))
