"""InProcessComms — the local, single-process transport (the default).

Used by: yaah.build.build() (registers nodes by role) and the harness (request/
publish) for local runs and tests; chosen by the runtime's `inproc` transport.
Where: local development and the test suite.
Why: zero-infra Comms — nodes are held in-process and routed by exact role name;
the same harness runs over it or over LocalBus/NatsComms unchanged.

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional, Tuple

from ..core import Envelope, Node, NodeConfig
from .comms import Comms, Handler
from .subscription import InMemorySubscription


class InProcessComms(Comms):
    def __init__(self) -> None:
        self._nodes: Dict[str, Tuple[Node, NodeConfig]] = {}
        self._subs: Dict[str, List[Handler]] = {}

    def register(self, target: str, node: Node, config: Optional[NodeConfig] = None) -> None:
        self._nodes[target] = (node, config or NodeConfig())

    async def request(self, target: str, envelope: Envelope) -> Envelope:
        try:
            node, config = self._nodes[target]
        except KeyError:
            raise LookupError("no node registered for target {!r}".format(target)) from None
        return await node.invoke(envelope, config)

    async def publish(self, topic: str, envelope: Envelope) -> None:
        # return_exceptions=True so one failing subscriber can't abort the
        # publisher or starve siblings (bug review H2). publish is fire-and-forget
        # by contract — a broken trace sink must never crash the pipeline run.
        handlers = list(self._subs.get(topic, ()))
        if handlers:
            await asyncio.gather(*(h(envelope) for h in handlers), return_exceptions=True)

    async def subscribe(self, topic: str, handler: Handler) -> InMemorySubscription:
        # async to match the Protocol (NATS subscribe is genuinely async) — callers
        # `await comms.subscribe(...)` uniformly; the in-proc body is just a dict append.
        handlers = self._subs.setdefault(topic, [])
        handlers.append(handler)
        return InMemorySubscription(topic=topic, _handlers=handlers, _handler=handler)
