"""ScriptedBackend — a deterministic ModelBackend keyed by model name.

Used by: multi-stage offline runs (the runtime's `fake_scripted` provider) and
tests, where each stage needs its own canned output.
Where: offline pipelines with more than one agent.
Why: FakeBackend is one shared sequence; this returns per-model sequences, so
`fake:spec` and `fake:eval` get different canned outputs.

CURSOR (assessment #7 / cluster 3 B2): the turn index is the MAX of two
sources, so the backend stays correct in the same-process case AND degrades
gracefully across cross-process resume:

  - same-process / in-memory: counts complete() calls per model. Always
    correct (no assumption about prompt content).
  - content-derived: counts how many prior emissions for this model appear
    verbatim in the prompt. Durable across resume: a rebuilt backend reading
    turn N's prompt sees N-1 prior responses in the transcript and advances
    to seq[N]. Heuristic — only works when the dialogue embeds prior
    responses verbatim. For grill-style dialogues whose transcript paraphrases
    rather than quotes, USE THE IN-PROCESS GATE-DRIVER instead of cross-
    process `--resume`; the assessment notes this is the supported path
    (drive grill in-process, not via `--resume`).

EXHAUSTION (assessment cluster 3 B2): when the cursor passes the end of the
sequence, return `self._default` — same shape as FakeBackend, so the offline
defaults aren't three answers to one question (FakeBackend → default,
scripted_tool_backend → distinct default, this one used to repeat seq[-1]).
Callers wanting loud failure pass `on_exhaustion="raise"`; `"repeat_last"`
restores the old behavior.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence


class ScriptedBackend:
    def __init__(self, by_model: Dict[str, Sequence[str]], default: str = "",
                 *, on_exhaustion: str = "default") -> None:
        if on_exhaustion not in ("default", "raise", "repeat_last"):
            raise ValueError(
                "on_exhaustion must be 'default'|'raise'|'repeat_last', got {!r}"
                .format(on_exhaustion))
        # A bare-string value means ONE reply, not a per-character script —
        # list("{...}") silently exploding into chars was the worst first-run
        # trap (every attempt answers `{`, every validator says not_json).
        self._by = {k: [v] if isinstance(v, str) else list(v)
                    for k, v in by_model.items()}
        self._i: Dict[str, int] = {k: 0 for k in self._by}
        self._default = default
        self._on_exhaustion = on_exhaustion

    async def complete(self, prompt: str, *, model: Optional[str] = None, **opts: Any) -> str:
        m = model or ""
        seq = self._by.get(m)
        if not seq:
            return self._default
        process_cursor = self._i.get(m, 0)
        content_cursor = sum(1 for s in seq if s and s in prompt)
        turn = max(process_cursor, content_cursor)
        if turn < len(seq):
            self._i[m] = turn + 1   # advance same-process counter (idempotent vs content)
            return seq[turn]
        if self._on_exhaustion == "raise":
            raise IndexError(
                "ScriptedBackend exhausted for model {!r} (seq has {} entries)"
                .format(model, len(seq)))
        if self._on_exhaustion == "repeat_last":
            return seq[-1]
        return self._default
