"""Attacher — the port for in-flight payload data from an agent's tracer span.

Used by: `AttachingAgent` (the wrapper) and `build/builders.py::_build_agent`
(resolves `attach: [...]` config items into Attacher instances).
Where: the harness layer's seam for "data the tracer already captured,
surfaced to in-flight decision-making." See ADR-0003.
Why: lets a pipeline branch / budget / route on data observed during the
agent's call without scraping the trace JSONL from a sidecar transform.

Engine boundary: the engine ships ZERO built-in attachers. The contribution
is the port + the wrapper + the tracer extension. All implementations live
in consumer code as `fn:module:func` references — same idiom transforms use.
The canonical reference implementation is `examples/arch-drift/transforms.py`
(copy-paste into your own transforms file).

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..core import Envelope


class Attacher:
    """Read an agent's recent execution and return payload keys to merge.
    Pure: no I/O, no side effects.

    Subclass and override `attach()`. Set `name` to a short human label (used
    in error messages). Set `requires_capture` to the tuple of tracer capture
    names the implementation needs (e.g. `("cost",)` for an attacher that
    reads tokens) — the builder enforces these are configured on the tracer,
    so the attacher never silently returns {}.
    """
    name: str = ""
    requires_capture: tuple = ()

    def attach(self, envelope: Envelope,
               span: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Return a dict whose keys will be merged onto the envelope's payload.
        `span` is the tracer's last model_call span for this correlation (a
        projected record dict) or None if no such span exists.
        Return {} to attach nothing for this call."""
        raise NotImplementedError
