"""Transforms for the regulation-watch pipeline.

One envelope-style transform (call: "envelope"):
  tally — after the judge scores the current draft, maintain the cycle count,
          decide whether to loop back to `draft` or fall through to the gate,
          and forward the judge's notes as loop guidance.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict


def tally(envelope, config) -> Dict[str, Any]:
    """Loop bookkeeping after the judge verdict.

    Reads from payload:
      cycle (int >= 0)  — attempts so far (0 on first pass, seeded in input)
      verdict (str)     — judge's "pass" | "revise"
      notes (str)       — judge's one-line feedback
      summary (str)     — current draft summary (carried by judge)
      impact_area (str) — from classify (carried through)

    Writes to payload:
      cycle         — incremented by 1
      loop_done      — "yes" to exit the loop (verdict pass OR max_cycles hit), else "no"
      loop_feedback  — judge's notes, forwarded as loop guidance to the draft agent
      digest_summary — the current summary, renamed so the gate and digest render
                       read a transform-provided key rather than a raw agent key

    config.extras:
      max_cycles (int, default 3) — hard cap on refine attempts
    """
    p = envelope.payload
    extras = config.extras or {}
    max_cycles = int(extras.get("max_cycles", 3))

    cycle = int(p.get("cycle", 0)) + 1
    verdict = str(p.get("verdict", "pass"))
    loop_done = "yes" if (verdict == "pass" or cycle >= max_cycles) else "no"

    return {
        **p,
        "cycle": cycle,
        "loop_done": loop_done,
        "loop_feedback": p.get("notes", ""),
        "digest_summary": p.get("summary", ""),
    }
