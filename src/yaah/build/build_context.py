"""BuildContext — shared dependencies handed to node builders.

Used by: every node builder (to reach the comms, the shared model backend, the
prompt source, and the config base dir).
Where: created in build() / serve_from_config(), passed into Registry.build().
Why: give builders what they need without globals.

Targets Python 3.9+.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..agents import ModelBackend
from ..comms import Comms


@dataclass
class BuildContext:
    comms: Comms
    backend: Optional[ModelBackend] = None       # shared model backend for 'agent' nodes
    prompt_source: Optional[Any] = None          # shared prompt source (yaah.prompts.PromptSource)
    data_source: Optional[Any] = None            # shared data source for 'get' nodes (yaah.data.DataSource)
    data_sink: Optional[Any] = None              # shared data sink for 'post' nodes (yaah.data.DataSink)
    mcp_source: Optional[Any] = None             # shared MCP-config source for agents (yaah.mcp.McpSource)
    idempotency_store: Optional[Any] = None      # for `idempotent: true` nodes (yaah.store.IdempotencyStore)
    tracer: Optional[Any] = None                 # injected Tracer for stage/model/tool spans (yaah.trace.Tracer)
    base_dir: Optional[str] = None               # resolve relative file paths (e.g. render templates)
