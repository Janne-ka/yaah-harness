"""NodeConfig — the per-node settings ("configurable innards").

Used by: every node's invoke(input, config); built by yaah.build from a node's
config entry (model, effort, etc.).
Where: passed alongside the Envelope into each node.
Why: keep node behaviour (model, effort, temperature, timeouts/retries,
node-specific extras) as data the harness threads in, not hardcoded in the node.

Targets Python 3.9+.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class NodeConfig:
    model: Optional[str] = None
    effort: Optional[str] = None  # 'low' | 'med' | 'high' | 'max'
    temperature: Optional[float] = None
    timeout: Optional[float] = None  # seconds
    retries: int = 0
    idempotency_key: Optional[str] = None
    extras: Dict[str, Any] = field(default_factory=dict)  # node-specific settings
