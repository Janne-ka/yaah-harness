"""StaticPromptSource — in-memory prompts.

Used by: tests and inline/default prompts; the runtime's `static` source.
Where: anywhere prompts are held in-process.
Why: the simplest PromptSource — a dict of key → template.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .prompt_source import PromptSource


class StaticPromptSource(PromptSource):
    def __init__(self, prompts: Optional[Dict[str, str]] = None) -> None:
        self._prompts = dict(prompts or {})

    def put(self, key: str, text: str) -> None:
        self._prompts[key] = text

    async def get(self, key: str, **opts: Any) -> str:
        try:
            return self._prompts[key]
        except KeyError:
            raise LookupError("no prompt {!r}".format(key)) from None
