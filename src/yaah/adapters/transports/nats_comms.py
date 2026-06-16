"""NatsComms — the real distributed transport, over a NATS server.

Used by: the runtime's `nats` transport; the harness (request/publish) on the
orchestrator, and worker processes (serve_node) host nodes.
Where: real cross-process / cross-machine runs.
Why: same model as LocalBus on a real broker — request = NATS request/reply,
publish/subscribe = pub/sub. `nats-py` is imported lazily, so it's only required
if this transport is used. (subscribe/serve are async here — network setup.)

Connecting to a REMOTE broker means auth + encryption: pass user/password, a
token, or an NKEY/JWT credentials file, and a TLS context. These are the same
options a cloud node would use; nothing about the harness changes.

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from ...core import Envelope, Kind, Node, NodeConfig

RequestHandler = Callable[[Envelope], Awaitable[Envelope]]
EventHandler = Callable[[Envelope], Awaitable[None]]


class _NatsSubscription:
    """Sync `.cancel()` over a NATS native subscription so the harness can drop
    a subscription the same way it does for InProcessComms/LocalBus (which
    return a `Subscription` dataclass with `.cancel()`). NATS's native
    `unsubscribe()` is async; cancel fires it as a background task so callers
    stay sync — fine because subscription teardown is best-effort cleanup."""

    def __init__(self, native: Any) -> None:
        self._native = native

    def cancel(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop — broker shutdown will reap it
        loop.create_task(self._native.unsubscribe())


class NatsComms:
    def __init__(self, servers: str = "nats://127.0.0.1:4222", *, request_timeout: float = 300.0,
                 user: Optional[str] = None, password: Optional[str] = None,
                 token: Optional[str] = None, creds: Optional[str] = None,
                 tls: Any = None, tls_hostname: Optional[str] = None) -> None:
        self._servers = servers
        # Default request/reply deadline. LLM nodes (a real `claude -p`) can take
        # MINUTES, far past NATS's usual sub-second RPC assumption — so the default
        # is 300s for the stated primary use case (agent pipelines), not the 30s
        # that would time out mid-pipeline (bug review M1). The deployment config can
        # still override per environment.
        self._request_timeout = request_timeout
        # Auth/encryption for a remote broker. None of these are set for a plain
        # local broker; all of them are just passed through to nats.connect.
        self._user = user
        self._password = password
        self._token = token
        self._creds = creds            # path to an NKEY/JWT credentials file
        self._tls = tls                # an ssl.SSLContext
        self._tls_hostname = tls_hostname
        self._nc: Any = None

    async def connect(self) -> "NatsComms":
        import nats  # lazy: only needed if this transport is used

        opts: dict = {}
        for key, val in (("user", self._user), ("password", self._password),
                         ("token", self._token), ("user_credentials", self._creds),
                         ("tls", self._tls), ("tls_hostname", self._tls_hostname)):
            if val is not None:
                opts[key] = val
        self._nc = await nats.connect(self._servers, **opts)
        return self

    async def request(self, target: str, envelope: Envelope, *, timeout: Optional[float] = None) -> Envelope:
        if timeout is None:
            timeout = self._request_timeout
        msg = await self._nc.request(target, envelope.to_json().encode(), timeout=timeout)
        return Envelope.from_json(msg.data.decode())

    async def publish(self, topic: str, envelope: Envelope) -> None:
        await self._nc.publish(topic, envelope.to_json().encode())

    async def subscribe(self, topic: str, handler: EventHandler) -> Any:
        async def cb(msg: Any) -> None:
            await handler(Envelope.from_json(msg.data.decode()))

        return _NatsSubscription(await self._nc.subscribe(topic, cb=cb))

    async def serve(self, subject: str, handler: RequestHandler, *, queue: Optional[str] = None) -> Any:
        async def cb(msg: Any) -> None:
            # Assessment cluster 2 B5: if `handler` raised, the old code let the
            # exception propagate up to NATS's dispatcher — the caller never got
            # a reply and blocked until `request_timeout` (default 300s) then saw
            # a GENERIC timeout instead of the actual error. LocalBus surfaces
            # the exception via the in-process Comms — transports must behave the
            # same on the error path. We now CATCH and reply with a Kind.ERROR
            # envelope carrying the exception repr so the caller fails fast.
            # Parsing is INSIDE the try (assessment #12): a malformed wire
            # payload used to escape this callback into NATS's dispatcher — the
            # caller waited out the full request_timeout and saw a generic
            # timeout instead of the parse error. With no decoded request to
            # reply from, correlation rides the NATS reply inbox alone.
            req: Optional[Envelope] = None
            try:
                req = Envelope.from_json(msg.data.decode())
                resp = await handler(req)
            except asyncio.CancelledError:
                raise  # teardown, not a node failure — never an ERROR reply
            except BaseException as e:
                resp = (req.reply_with(Kind.ERROR, {"error": repr(e)})
                        if req is not None
                        else Envelope(Kind.ERROR, {"error": repr(e)}))
            if msg.reply:
                await self._nc.publish(msg.reply, resp.to_json().encode())

        # A queue group makes the broker deliver each request to exactly ONE
        # member, so N replicas of a node form a shared, load-balanced pool
        # (the shared-cloud-resource case) instead of all answering every request.
        if queue:
            return await self._nc.subscribe(subject, queue=queue, cb=cb)
        return await self._nc.subscribe(subject, cb=cb)

    async def serve_node(self, role: str, node: Node, config: Optional[NodeConfig] = None,
                         *, queue: Optional[str] = None) -> Any:
        cfg = config or NodeConfig()

        async def handler(env: Envelope) -> Envelope:
            return await node.invoke(env, cfg)

        # default the queue group to the role: every process serving this role
        # joins one pool, so requests load-balance across replicas (and multiple
        # harness networks calling the shared node all draw from the same pool).
        return await self.serve(role, handler, queue=queue or role)

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.drain()
            self._nc = None
