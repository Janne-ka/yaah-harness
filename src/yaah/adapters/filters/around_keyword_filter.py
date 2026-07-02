"""AroundKeywordFilter — return ±N lines around occurrences of a keyword in the
value (R10 EXTENDED).

Used by: an agent's envelope_get with `filter: {name: "around", keyword: "...",
n: 3}`. Configured per agent via the pipeline JSON `filters:` allow-list.
Where: when the model wants a grep-like slim view of a large text field (a long
diff, a file dump) instead of the whole value plus a hard truncation.
Why: cheaper than fetching the whole field and trimming downstream. Adjacent
windows merge so two nearby hits don't repeat overlapping context.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any
from ...filters import Filter


class AroundKeywordFilter(Filter):
    async def apply(self, value: Any, *, keyword: str = "", n: int = 3,
                    all: bool = False, **_: Any) -> Any:
        """Return ±n lines around occurrences of `keyword` in `value`.

        Args:
            value: any value — non-string values pass through unchanged.
            keyword: literal substring; empty keyword returns value verbatim.
            n: lines of context on each side of each hit (default 3).
            all: if True, return all hits with merged overlapping windows
                separated by `"..."`; if False (default), only the first hit.
        """
        if not isinstance(value, str) or not keyword:
            return value
        lines = value.splitlines()
        hits = [i for i, ln in enumerate(lines) if keyword in ln]
        if not hits:
            return ""
        if not all:
            hits = hits[:1]
        ranges = []
        for h in hits:
            start = max(0, h - int(n))
            end = min(len(lines), h + int(n) + 1)
            if ranges and ranges[-1][1] >= start:
                ranges[-1] = (ranges[-1][0], end)
            else:
                ranges.append((start, end))
        parts = []
        for i, (s, e) in enumerate(ranges):
            if i > 0:
                parts.append("...")
            parts.extend(lines[s:e])
        return "\n".join(parts)
