"""Failure — one item in a Verdict's failure list.

Used by: validators (to describe why output failed) and the harness retry loop
(folds fix_hint back into the worker's next input).
Where: inside Verdict.failures.
Why: a structured reason (code + message + fix hint) instead of a bare string.

Targets Python 3.9+.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Failure:
    code: str
    message: str
    fix_hint: Optional[str] = None
    # Optional machine-readable payload for a code that carries structured fields
    # (e.g. render_unfilled_placeholders puts its unfilled key list here). Default
    # empty so every existing 3-arg / 2-arg constructor and caller is unchanged —
    # a debugger reads `data` instead of regex-scraping the prose `message`.
    data: Dict[str, Any] = field(default_factory=dict)

    # Two shapes produced by every JSON-consuming node (the agent's own output_schema
    # self-check and the json_object/json_schema validators). The comments at those sites
    # note "the two paths can't diverge"; these factories LOCK that — one `code` string and
    # (for schema_mismatch) the `errors[:8]` cap the harness retry loop reads — while each
    # caller keeps its own subject/fix_hint. See the three sites in agents/agent.py and
    # validators/.

    def to_dict(self) -> Dict[str, Any]:
        """The canonical JSON shape of one failure — code/message/fix_hint, plus
        `data` ONLY when non-empty (so minimal failures stay minimal and a
        `Failure(**d)` round-trip is clean). The single source of truth both the
        Verdict envelope payload and the run/MCP failure JSON emit."""
        d: Dict[str, Any] = {"code": self.code, "message": self.message,
                             "fix_hint": self.fix_hint}
        if self.data:
            d["data"] = self.data
        return d

    @classmethod
    def not_json(cls, exc: object, *, subject: str = "output",
                 fix_hint: str = "return a single JSON object",
                 sample: str = "") -> "Failure":
        """`raw` didn't parse as JSON (extract_json raised). `sample` (optional,
        caller-bounded) is appended so the failure NAMES what the producer
        actually said — "no JSON found" alone cannot distinguish an empty reply
        from a prose refusal, and that distinction is the whole diagnosis (the
        2026-08-07 A-arm storms were prose refusals, invisible until sampled)."""
        msg = "{} is not valid JSON: {}".format(subject, exc)
        if sample:
            msg = "{} — the output began: {}".format(msg, sample)
        return cls("not_json", msg, fix_hint)

    @classmethod
    def schema_mismatch(cls, errors: List[str], *,
                        fix_hint: str = "match the declared schema") -> "Failure":
        """`obj` parsed but violates the declared schema. `errors[:8]` — the cap the retry
        loop folds back as feedback — is fixed here so the sites can't disagree on it."""
        return cls("schema_mismatch", "; ".join(errors[:8]), fix_hint)
