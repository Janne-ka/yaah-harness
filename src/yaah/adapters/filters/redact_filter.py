"""RedactFilter — replace regex-matched patterns in a string value with a
placeholder (R10 EXTENDED).

Used by: an agent's envelope_get with `filter: {name: "redact"}` after the
author wires `filters: {redact: {type: "redact", patterns: [...]}}` in the
pipeline JSON.
Where: when the canonical envelope must carry secrets/PII (other agents may
need them) but a particular slim-agent view should not. Composable with
AroundKeywordFilter.
Why: the AUTHOR pins the patterns (constructor), the model can only NAME the
filter — so a malicious or careless model can't widen the redaction policy.
That's the allow-list rule, applied to params too.

Targets Python 3.9+.
"""
from __future__ import annotations

import re
from typing import Any, List, Optional


class RedactFilter:
    def __init__(self, default_patterns: Optional[List[str]] = None,
                 replacement: str = "[REDACTED]") -> None:
        """Build a RedactFilter.

        Args:
            default_patterns: list of regex strings the AUTHOR pins at
                construction. SECURITY: the model's `filter: {name: "redact"}`
                invocation CANNOT override these — `apply` ignores any
                model-supplied `patterns` so the model can neither WIDEN nor
                NARROW the policy. The allow-list rule applied to filter params.
            replacement: string substituted for each match (default
                `"[REDACTED]"`). The model also cannot override this.
        """
        self._patterns = list(default_patterns or [])
        self._replacement = replacement
        self._compiled = [self._compile(p) for p in self._patterns]

    @staticmethod
    def _compile(p: str):
        try:
            return re.compile(p)
        except re.error:
            return None

    async def apply(self, value: Any, **_: Any) -> Any:
        if not isinstance(value, str) or not self._compiled:
            return value
        out = value
        for pat in self._compiled:
            if pat is not None:
                out = pat.sub(self._replacement, out)
        return out
