"""build_filter — resolve a `filters:` spec from pipeline JSON into a Filter
instance (R10).

Used by: `build/builders._build_agent` when the agent node declares
`filters: {<name>: {type: ..., ...args}}`.
Where: the pipeline-JSON → runtime seam, sibling to node builders.
Why: keep `if type == "..."` dispatch in ONE place so adding a filter adapter
means one row here, not a change threaded across builders / agent / tool.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict

from .filters import Filter


def build_filter(spec: Dict[str, Any], *, comms: Any = None) -> Filter:
    kind = spec.get("type")
    if kind is None:
        raise ValueError("filter spec needs 'type' (got {!r})".format(spec))
    if kind == "around_keyword":
        from .adapters.filters.around_keyword_filter import AroundKeywordFilter
        return AroundKeywordFilter()
    if kind == "redact":
        from .adapters.filters.redact_filter import RedactFilter
        return RedactFilter(
            default_patterns=spec.get("patterns") or [],
            replacement=spec.get("replacement", "[REDACTED]"),
        )
    if kind == "call_target":
        from .adapters.filters.call_target_filter import CallTargetFilter
        target = spec.get("target")
        if not target:
            raise ValueError("call_target filter needs 'target'")
        return CallTargetFilter(target, comms=comms)
    raise ValueError("unknown filter type {!r}".format(kind))
