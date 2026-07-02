"""HttpPromptSource — fetch prompts over HTTP.

Used by: deployments that serve prompts from a URL (the runtime's `http` source).
Where: hosts with network access to a prompt service.
Why: a cloud prompt store behind one URL; httpx is imported lazily so it's only
required if this source is used.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional
from ...prompts import PromptSource

# Mirrors external_call's _DEFAULT_HTTP_TIMEOUT (assessment cluster 5 security #1).
# `httpx.AsyncClient()` defaults to no timeout — a misbehaving prompt server
# would hang the agent indefinitely. 30s is generous for a prompt fetch (way
# more than any reasonable cloud-prompt-store round-trip). Override per call.
_DEFAULT_TIMEOUT = 30.0


class HttpPromptSource(PromptSource):
    def __init__(self, base_url: str, *,
                 fetch: Optional[Callable[..., Awaitable[str]]] = None,
                 timeout: Optional[float] = None, **opts: Any) -> None:
        # `fetch` is the external dependency, injected for testability: an async
        # (url, **opts) -> body-text callable. Defaults to a real httpx GET
        # (imported lazily, raises for non-2xx). Tests pass a stub so this source
        # runs without httpx / network.
        self._base = base_url.rstrip("/")
        self._fetch = fetch
        self._opts = opts
        self._timeout = _DEFAULT_TIMEOUT if timeout is None else timeout

    async def get(self, key: str, **opts: Any) -> str:
        url = "{}/{}".format(self._base, key)
        fetch = self._fetch or self._httpx_get  # injected stub, else the real GET
        merged_opts = {"timeout": self._timeout, **self._opts, **opts}
        return await fetch(url, **merged_opts)

    @staticmethod
    async def _httpx_get(url: str, **opts: Any) -> str:  # pragma: no cover - real network shim
        import httpx  # lazy

        async with httpx.AsyncClient() as client:
            r = await client.get(url, **opts)
            r.raise_for_status()
            return r.text
