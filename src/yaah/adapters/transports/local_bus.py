"""LocalBus — an in-process bus that is faithful to the wire.

Used by: tests/examples and the runtime's `localbus` transport; the harness
talks to it exactly like any Comms.
Where: offline proof of the distributed model.
Why: JSON-serialize every envelope on each hop, reach nodes by SUBJECT NAME
(not object identity), and decouple request/reply via per-subject queues and
reply inboxes — so the same build()/Harness runs over it by swapping the
transport, demonstrating comms↔harness separation without a real broker.

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, List, Optional

from ...comms import Comms, Handler, InMemorySubscription
from ...core import Envelope, Node, NodeConfig

RequestHandler = Callable[[Envelope], Awaitable[Envelope]]


class LocalBus(Comms):
    def __init__(self) -> None:
        self._queues: Dict[str, "asyncio.Queue"] = {}
        self._servers: Dict[str, "asyncio.Future"] = {}
        self._inboxes: Dict[str, "asyncio.Future"] = {}
        self._subs: Dict[str, List[Handler]] = {}
        self._seq = 0

    # -- server side --

    def serve(self, subject: str, handler: RequestHandler) -> None:
        q: "asyncio.Queue" = asyncio.Queue()
        self._queues[subject] = q
        self._servers[subject] = asyncio.ensure_future(self._serve_loop(q, handler))

    def register(self, role: str, node: Node, config: Optional[NodeConfig] = None) -> None:
        cfg = config or NodeConfig()

        async def handler(env: Envelope) -> Envelope:
            return await node.invoke(env, cfg)

        self.serve(role, handler)

    async def _serve_loop(self, q: "asyncio.Queue", handler: RequestHandler) -> None:
        while True:
            wire, reply_subject = await q.get()
            try:
                resp = await handler(Envelope.from_json(wire))  # deserialize, invoke
                self._resolve(reply_subject, resp.to_json(), None)  # serialize reply
            except Exception as exc:  # surface the failure to the caller
                self._resolve(reply_subject, None, exc)
            finally:
                q.task_done()

    def _resolve(self, reply_subject: str, wire: Optional[str], exc: Optional[BaseException]) -> None:
        fut = self._inboxes.pop(reply_subject, None)
        if fut is not None and not fut.done():
            if exc is not None:
                fut.set_exception(exc)
            else:
                fut.set_result(wire)

    # -- client side (Comms) --

    async def request(self, target: str, envelope: Envelope) -> Envelope:
        q = self._queues.get(target)
        if q is None:
            raise LookupError("no server on subject {!r}".format(target))
        self._seq += 1
        reply_subject = "_inbox.{}".format(self._seq)
        fut: "asyncio.Future" = asyncio.get_running_loop().create_future()
        self._inboxes[reply_subject] = fut
        try:
            await q.put((envelope.to_json(), reply_subject))  # serialize on the way out
            wire = await fut
        finally:
            # always drop the inbox entry — on reply (_resolve already popped it,
            # so this is a no-op), but crucially also on cancellation/timeout,
            # where nothing else would remove it (the leak in early_review #4).
            self._inboxes.pop(reply_subject, None)
        return Envelope.from_json(wire)

    async def publish(self, topic: str, envelope: Envelope) -> None:
        # return_exceptions=True: a failing subscriber must not abort the publisher
        # or starve siblings (bug review H2) — publish is fire-and-forget.
        wire = envelope.to_json()
        handlers = list(self._subs.get(topic, ()))
        if handlers:
            await asyncio.gather(*(h(Envelope.from_json(wire)) for h in handlers),
                                 return_exceptions=True)

    async def subscribe(self, topic: str, handler: Handler) -> InMemorySubscription:
        # async to match the Comms Protocol (NATS subscribe is async); body is local.
        handlers = self._subs.setdefault(topic, [])
        handlers.append(handler)
        return InMemorySubscription(topic=topic, _handlers=handlers, _handler=handler)

    async def close(self) -> None:
        for task in self._servers.values():
            task.cancel()
        self._servers.clear()
        self._queues.clear()
        self._inboxes.clear()
