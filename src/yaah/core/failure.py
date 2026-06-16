"""Failure — one item in a Verdict's failure list.

Used by: validators (to describe why output failed) and the harness retry loop
(folds fix_hint back into the worker's next input).
Where: inside Verdict.failures.
Why: a structured reason (code + message + fix hint) instead of a bare string.

Targets Python 3.9+.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Failure:
    code: str
    message: str
    fix_hint: Optional[str] = None
