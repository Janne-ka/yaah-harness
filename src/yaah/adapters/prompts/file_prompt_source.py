"""FilePromptSource — read prompts from a directory.

Used by: apps whose prompts live as .md files, and the runtime's `file` source.
Where: local prompt files.
Why: keep prompts as plain, editable, git-diffable files — get('eval') reads
<base_dir>/eval<ext>.

Caches by modification time (early_review #5): a re-read only happens when the
file actually changes, so repeated invokes + retries don't re-read every time,
yet editing a prompt is still picked up immediately (hot-reload preserved — the
daily-iteration requirement). The cache is bounded by the number of prompt files.

Targets Python 3.9+.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Tuple

from ...safepath import safe_join
from ...prompts import PromptSource


class FilePromptSource(PromptSource):
    def __init__(self, base_dir: str, ext: str = ".md") -> None:
        self._base = base_dir
        self._ext = ext
        self._cache: Dict[str, Tuple[float, str]] = {}  # path -> (mtime, content)

    async def get(self, key: str, **opts: Any) -> str:
        # safe_join (assessment cluster 5 security #3): a relative `key` like
        # `../../../etc/passwd` previously resolved straight through. Absolute
        # keys still pass through (operator's explicit intent).
        path = safe_join(self._base, key if os.path.isabs(key) else key + self._ext)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        cached = self._cache.get(path)
        if cached is not None and mtime is not None and cached[0] == mtime:
            return cached[1]  # unchanged since last read
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if mtime is not None:
            self._cache[path] = (mtime, content)
        return content
