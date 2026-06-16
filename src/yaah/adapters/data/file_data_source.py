"""FileDataSource — fetch a file, optionally only a line range (± N context).

Used by: the 'get' node (GetNode) and the runtime's `file` data source.
Where: local files.
Why: the file counterpart to GitDiffSource's hunks — get('path', start=120,
end=160) returns just those lines, or center=140 + radius=20 returns ±20 lines
around a point. Same motive: hand a stage the slice it needs, not the whole file.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Optional

from ...safepath import safe_join


class FileDataSource:
    def __init__(self, base_dir: str = "") -> None:
        self._base = base_dir

    async def fetch(self, key: str, *, start: Optional[int] = None, end: Optional[int] = None,
                    center: Optional[int] = None, radius: Optional[int] = None,
                    cwd: Optional[str] = None, **_: Any) -> str:
        base = cwd or self._base
        # safe_join (assessment cluster 5 security #3): a relative `key` like
        # `../../etc/passwd` would otherwise escape base. Absolute keys are
        # allowed (operator's explicit intent; trusted-config model).
        path = safe_join(base, key)
        # Fail loud on half-spec center/radius (assessment cluster 5 #3) — used
        # to silently fall through and return the entire file.
        if (center is None) != (radius is None):
            raise ValueError(
                "center and radius must be set together (got center={!r}, radius={!r})"
                .format(center, radius))
        # 1-based inclusive `start` -> 0-based slice. Reject start=0 explicitly
        # — line 0 doesn't exist; the old code silently treated it as line 1.
        # Checked BEFORE the center/radius expansion: an EXPLICIT bad start is a
        # caller error, but a COMPUTED center - radius near the top of a file
        # legitimately underflows and clamps to line 1 (assessment #11).
        if start is not None and start <= 0:
            raise ValueError("start must be >= 1 (got {})".format(start))
        if center is not None:
            start, end = max(1, center - radius), center + radius
        if start is None and end is None:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        lo = max(0, (start or 1) - 1)
        hi = len(lines) if end is None else max(lo, min(len(lines), end))
        return "".join(lines[lo:hi])
