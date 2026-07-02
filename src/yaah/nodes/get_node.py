"""GetNode — fetch data via a DataSource and add it to the payload.

Used by: yaah.build (the 'get' node type). Typical use: pull a slice of data
(a file, an HTTP response, a configured fixture) into a payload key so a
downstream stage can read it without doing its own I/O.
Where: any stage that needs to pull a slice of data into the flow.
Why: keep "what data, how much, from where" as config (source key + fetch opts),
not as bespoke code in each stage — the data counterpart to an Agent fetching
its prompt from a PromptSource. The COMMAND/source is trusted config; the cwd
(worktree) is per-run payload data (cwd_from). Unlike ShellNode this carries the
whole input payload forward and just adds `into`, so run context (workdir) sticks.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from ..core import Envelope, NodeConfig
from ..cwd import resolve_cwd


class GetNode:
    def __init__(self, data_source: Any, key: str, *, into: str = "data",
                 cwd_from: Optional[str] = None, context: Optional[int] = None,
                 paths: Optional[Sequence[str]] = None) -> None:
        if data_source is None:
            raise ValueError("a 'get' node needs a data source; pass data_source= to build()")
        self._source = data_source
        self._key = key
        self._into = into
        self._cwd_from = cwd_from
        self._context = context
        self._paths = list(paths) if paths else None

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        opts: dict = {}
        cwd = resolve_cwd(input, self._cwd_from)
        if cwd:
            opts["cwd"] = cwd
        if self._context is not None:
            opts["context"] = self._context
        if self._paths:
            opts["paths"] = self._paths
        data = await self._source.fetch(self._key, **opts)
        payload = dict(input.payload)  # enrich, don't replace — keep workdir etc.
        payload[self._into] = data
        return input.reply_with("result", payload)
