"""PostNode — write a payload field out via a DataSink (the 'post' transform).

Used by: yaah.build (the 'post' node type). The write mirror of GetNode: where
GetNode pulls data into the flow, PostNode pushes a field out — to a file, a
substrate store, an external system. memory-write is a `post`.
Where: a stage that persists/emits an artifact (a report, a result, a scratchpad).
Why: keep "what to write, where" as config (sink key + which field), not bespoke
code. The sink/key is trusted config; the cwd (worktree) is per-run payload data
(cwd_from). Carries the input payload forward and records the returned handle
under `into`, so a run context (workdir) and the locator both survive downstream.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Optional

from ..core import Node, Envelope, NodeConfig
from ..cwd import resolve_cwd


class PostNode(Node):
    def __init__(self, data_sink: Any, key: str, *, field: str = "data",
                 into: str = "stored", cwd_from: Optional[str] = None) -> None:
        if data_sink is None:
            raise ValueError("a 'post' node needs a data sink; pass data_sink= to build()")
        self._sink = data_sink
        self._key = key
        self._field = field
        self._into = into
        self._cwd_from = cwd_from

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        if self._field not in input.payload:
            raise KeyError("post node: payload has no field {!r} to write".format(self._field))
        opts: dict = {}
        cwd = resolve_cwd(input, self._cwd_from)
        if cwd:
            opts["cwd"] = cwd
        handle = await self._sink.store(self._key, input.payload[self._field], **opts)
        payload = dict(input.payload)  # enrich, don't replace — keep run context
        payload[self._into] = handle
        return input.reply_with("result", payload)
