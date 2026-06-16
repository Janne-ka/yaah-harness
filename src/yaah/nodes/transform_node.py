"""TransformNode — call an external capability as a transform (tools + mcp).

Used by: yaah.build (the 'transform' node type). This is the ONE generic class
behind a HARNESS-initiated external call. It shares its resolver (`call_target`)
with the agent tool-loop, so a tool and a transform run through one code path.
Where: a stage that calls out (a function, another node, an HTTP endpoint).
Why: keep "what to call" as one config string (the target), input mapped to args
and the result mapped back into the payload. Uniform with get/post/agent.

This node is for HARNESS-initiated calls (a pipeline step the orchestrator routes
to). MODEL-initiated tools/MCP are NOT this node — they live in agent config (the
model decides to call them mid-reasoning; see docs/agent-tools.md). There is
deliberately no `mcp:` scheme: MCP is a model-tool protocol, so an MCP server is
agent config (claude native); calling one as a standalone step is just an API
call -> use `http:`/`fn:`.

Target schemes (via call_target): fn:module:func, node:role, http(s)://URL.

Two calling conventions (`call`):
  - "args" (default) — fn:`target`(args) → result nested under `into` (enrich,
    don't replace). The uniform get/post/agent shape; also the agent tool-loop path.
  - "envelope" — the fn:`target` receives the richer (envelope, config) and its
    result (a dict) SPREADS over the payload top-level (an Envelope passes through).
    This is the "python extends transform" specialization — it subsumed the former
    standalone `python` node, so a config-aware deterministic step is just a
    transform. Only valid for an fn: target (the others have no config to pass).

Targets Python 3.9+.
"""
from __future__ import annotations

import inspect
from typing import Any, Optional

from ..core import Envelope, NodeConfig
from ..external_call import call_target, import_callable


class TransformNode:
    def __init__(self, target: str, *, comms: Any = None, args_from: Optional[str] = None,
                 into: str = "result", call: str = "args") -> None:
        self._target = target
        self._comms = comms
        self._args_from = args_from   # payload key holding the args; default = whole payload
        self._into = into
        self._call = call             # "args" (fn(args)->into) | "envelope" (fn(envelope, config), spread)

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        if self._call == "envelope":
            return await self._invoke_envelope(input, config)
        if self._args_from is not None:
            # Assessment cluster 4 #6: a missing `args_from` key used to silently
            # call the target with {} — a programming error pretending to be a
            # silent-empty call. PostNode raises in the analogous case; align here
            # so the transform/post pair behaves consistently.
            if self._args_from not in input.payload:
                raise KeyError(
                    "transform args_from key {!r} not in payload (have: {})"
                    .format(self._args_from, sorted(input.payload)))
            args = input.payload[self._args_from]
        else:
            args = dict(input.payload)
        result = await call_target(self._target, args, comms=self._comms,
                                   timeout=config.timeout, reply_to=input)
        payload = dict(input.payload)  # enrich, don't replace — keep run context
        payload[self._into] = result
        return input.reply("result", **payload)

    async def _invoke_envelope(self, input: Envelope, config: NodeConfig) -> Envelope:
        """The envelope-style realization (subsumes the old python node): call the
        fn: target with (envelope, config) — so a deterministic step can read the
        whole envelope and its NodeConfig (e.g. `config.extras`) — and SPREAD its
        result over the payload top-level (so a following `branch` can read the keys
        it sets). A returned Envelope passes through unchanged; a dict is spread via
        reply_with (M4-safe: a `sender` key in the result can't collide)."""
        scheme, sep, rest = self._target.partition(":")
        if scheme != "fn":
            raise ValueError(
                "transform call='envelope' needs an fn: target (got {!r}); only a local "
                "function takes (envelope, config)".format(self._target))
        func = import_callable(rest)
        res = func(input, config)
        if inspect.isawaitable(res):
            res = await res
        if isinstance(res, Envelope):
            return res
        if isinstance(res, dict):
            return input.reply_with("result", res)  # spread top-level (reserved-key safe)
        raise TypeError(
            "transform fn (call='envelope') must return an Envelope or dict, got {}".format(
                type(res).__name__))
