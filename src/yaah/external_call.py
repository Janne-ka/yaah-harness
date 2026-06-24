"""call_target — resolve and invoke an external target by a scheme string.

Used by: `TransformNode` (a harness-initiated pipeline step) AND the agent
tool-loop (a model-initiated tool call). One resolver, two entry points — so "a
tool" and "a transform" run through the same code.
Where: the seam to anything outside a node — a local function, another node over
Comms, an HTTP endpoint.
Why: keep "what to call" a single config string; map args in, result out.

Schemes:
  fn:module:func   -> call a local callable(args) (sync or async); imported
                      relative to the config's directory (see docs/node-reference.md)
  node:role        -> Comms.request(role, args)
  http(s)://URL    -> POST args as JSON, parse the JSON reply (stdlib urllib)

(No `mcp:` — MCP is model-initiated agent config, not a target here; see
docs/agent-tools.md.)

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
from typing import Any, Optional

from .core import Envelope


async def call_target(target: str, args: Any, *, comms: Any = None,
                      timeout: Optional[float] = None, reply_to: Optional[Envelope] = None) -> Any:
    scheme, sep, rest = target.partition(":")
    if not sep:
        raise ValueError("target must be 'scheme:rest', got {!r}".format(target))
    if scheme == "fn":
        return await _call_fn(rest, args)
    if scheme == "node":
        return await _call_node(rest, args, comms, reply_to)
    if scheme in ("http", "https"):
        return await _call_http(target, args, timeout)
    raise ValueError(
        "target scheme {!r} not supported — use one of: 'fn:module:func', "
        "'node:role', 'http://...', 'https://...'".format(scheme))


def import_callable(path: str) -> Any:
    """Resolve a 'module:func' dotted path to the callable. Shared by `_call_fn`
    (the default fn: realization) and TransformNode's envelope-style realization
    (the one that subsumed the old python node), so both import a target the same
    way."""
    mod, sep, fn = path.partition(":")
    if not sep:
        raise ValueError("fn target must be 'module:func', got {!r}".format(path))
    return getattr(importlib.import_module(mod), fn)


async def _call_fn(path: str, args: Any) -> Any:
    func = import_callable(path)
    res = func(args)
    return await res if inspect.isawaitable(res) else res


async def _call_node(role: str, args: Any, comms: Any, reply_to: Optional[Envelope]) -> Any:
    if comms is None:
        raise ValueError(
            "a 'node:' target needs comms — pass comms= when calling "
            "call_target(); only fn:/http(s): targets work without it")
    # reply_with (dict payload), NOT reply(**args): a forwarded payload may contain
    # a `sender` key that would collide with reply()'s keyword arg (bug review M4).
    if isinstance(args, dict):
        req = reply_to.reply_with("task", dict(args)) if reply_to is not None else Envelope("task", dict(args))
    else:
        req = reply_to.reply_with("task", {"args": args}) if reply_to is not None else Envelope("task", {"args": args})
    resp = await comms.request(role, req)
    return resp.payload


# Default HTTP timeout when the caller didn't pass one (assessment cluster 4 #5):
# previously `timeout=None` meant "wait forever" — a misbehaving target would hang
# the calling stage indefinitely. 30s is generous for a config-targeted endpoint
# (way more than any reasonable JSON RPC) without being a hidden block. Override
# per call by passing `timeout=`.
_DEFAULT_HTTP_TIMEOUT = 30.0


async def _call_http(url: str, args: Any, timeout: Optional[float]) -> Any:
    import urllib.request  # stdlib; no dependency

    effective_timeout = _DEFAULT_HTTP_TIMEOUT if timeout is None else timeout

    def _post() -> Any:
        req = urllib.request.Request(
            url, data=json.dumps(args).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=effective_timeout) as r:  # nosec - target is trusted config
            body = r.read().decode(errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return body

    return await asyncio.to_thread(_post)
