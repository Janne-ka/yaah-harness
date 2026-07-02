"""Failure — one item in a Verdict's failure list.

Used by: validators (to describe why output failed) and the harness retry loop
(folds fix_hint back into the worker's next input).
Where: inside Verdict.failures.
Why: a structured reason (code + message + fix hint) instead of a bare string.

Targets Python 3.9+.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Failure:
    code: str
    message: str
    fix_hint: Optional[str] = None

    # Two shapes produced by every JSON-consuming node (the agent's own output_schema
    # self-check and the json_object/json_schema validators). The comments at those sites
    # note "the two paths can't diverge"; these factories LOCK that — one `code` string and
    # (for schema_mismatch) the `errors[:8]` cap the harness retry loop reads — while each
    # caller keeps its own subject/fix_hint. See the three sites in agents/agent.py and
    # validators/. (ADR-0006 slop-fix #4.)

    @classmethod
    def not_json(cls, exc: object, *, subject: str = "output",
                 fix_hint: str = "return a single JSON object") -> "Failure":
        """`raw` didn't parse as JSON (extract_json raised)."""
        return cls("not_json", "{} is not valid JSON: {}".format(subject, exc), fix_hint)

    @classmethod
    def schema_mismatch(cls, errors: List[str], *,
                        fix_hint: str = "match the declared schema") -> "Failure":
        """`obj` parsed but violates the declared schema. `errors[:8]` — the cap the retry
        loop folds back as feedback — is fixed here so the sites can't disagree on it."""
        return cls("schema_mismatch", "; ".join(errors[:8]), fix_hint)
