"""bounded — THE free-text length policy for anything that reaches a trace.

Used by: PhaseContributor (the `error` attr it projects) and ClaudeCliProvider
(the stderr tail it puts on an `error` event, which the harness then notes as a
failure detail and phase projects). Any new site that has to clip free text
before it can land in a record calls this instead of spelling its own slice.
Where: the engine tracing core — a pure string function, no I/O.
Why: every hand-rolled `text[:N]` is a policy nobody can find and half of them
forget the marker, so a clipped message reads as a complete one. One helper,
one marker, callers own only their limit.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any

#: Appended to any text this helper had to clip — the single convention, so an
#: operator reading a record can always tell "cut" from "that's all there was".
#: Sits at the cut, so it LEADS when the tail was kept and trails otherwise.
TRUNCATED_MARKER = "...[truncated]"


def bounded(text: Any, max_chars: int, *, keep: str = "head") -> str:
    """`text` as a string, clipped to `max_chars` with TRUNCATED_MARKER marking
    the cut. Non-strings are coerced (a caller passing an exception or a dict
    still gets a bounded string, never a TypeError).

    `keep` picks which END survives, because the diagnosis is not always in the
    same place: `"head"` (default) for a structured failure whose CODE leads —
    a validator verdict, a rejected reply; `"tail"` for a subprocess's stderr,
    where the banner scrolls past and the error that actually killed it is the
    last thing written."""
    s = text if isinstance(text, str) else str(text)
    if len(s) <= max_chars:
        return s
    if keep == "tail":
        return TRUNCATED_MARKER + s[len(s) - max_chars:]
    return s[:max_chars] + TRUNCATED_MARKER
