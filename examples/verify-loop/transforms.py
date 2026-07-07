"""Transforms for the verify-loop example pipeline.

Two envelope-style transforms (call: "envelope"):
  tally     — after the judge scores the current attempt, maintain cycle count,
               update the best-so-far result, and decide whether to loop or exit.
  emit_best — terminal stage: project the best attempt into a clean final payload.

Run-shape: produce → judge → tally (branches: loop_done="yes" → emit_best, else → produce).

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict


def tally(envelope, config) -> Dict[str, Any]:
    """After the judge scores the current attempt, update loop bookkeeping.

    Reads from payload:
      cycle (int ≥ 0)    — attempts so far (0 on first pass, seeded in input.json)
      best_score (int)   — highest score seen (-1 on first pass, seeded in input.json)
      best_artifact      — text of the best attempt so far ("" on first pass)
      artifact           — current attempt text (carried by judge, see pipeline JSON)
      score (int)        — judge's numeric score for the current attempt
      notes (str)        — judge's one-line feedback

    Writes to payload:
      cycle             — incremented by 1 (post-attempt counter)
      best_score        — updated if score > best_score
      best_artifact     — updated if score > best_score
      loop_done         — "yes" if score >= pass_threshold or cycle >= max_cycles
      loop_feedback     — judge's notes, forwarded as {{loop_feedback}} in the produce template.
                          Named `loop_feedback` (not `feedback`) to avoid colliding with the
                          engine's built-in `feedback` key, which the harness auto-appends to
                          the agent prompt when non-empty (causing double-injection).

    config.extras keys (set in the pipeline's node config: block):
      max_cycles (int, default 3)       — maximum attempts before forced exit
      pass_threshold (int, default 8)   — score at which the loop exits early
    """
    p = envelope.payload
    extras = config.extras or {}
    max_cycles = int(extras.get("max_cycles", 3))
    pass_threshold = int(extras.get("pass_threshold", 8))

    score = int(p.get("score", 0))
    cycle = int(p.get("cycle", 0)) + 1   # increment: this attempt is now complete
    best_score = int(p.get("best_score", -1))
    best_artifact = p.get("best_artifact", "")
    artifact = p.get("artifact", p.get("raw", ""))

    # Guard rail: the branch logic should stop the loop at max_cycles, but a
    # reducer bug could unbind it. Fail loudly before ScriptedProvider exhausts
    # or a real LLM burns tokens on phantom attempts.
    if cycle > max_cycles:
        raise RuntimeError(
            "verify-loop: cycle {} exceeds max_cycles {} — "
            "check tally branch logic or max_cycles config".format(cycle, max_cycles)
        )

    if score > best_score:
        best_score = score
        best_artifact = artifact

    loop_done = "yes" if (score >= pass_threshold or cycle >= max_cycles) else "no"

    return {
        **p,
        "cycle": cycle,
        "best_score": best_score,
        "best_artifact": best_artifact,
        "loop_done": loop_done,
        "loop_feedback": p.get("notes", ""),   # judge notes → {{loop_feedback}} in produce template
    }


def emit_best(envelope, config) -> Dict[str, Any]:
    """Terminal stage: surface the best attempt under predictable output names.

    best_artifact and best_score are already in the payload (maintained by tally,
    preserved across loop iterations by graph.sticky). This transform returns only
    the three named keys; graph.sticky will re-fold any sticky keys not present in
    the return dict (cycle, loop_feedback) — those will appear in the final payload
    too, which is fine. The winner keys are best_artifact and best_score.
    """
    p = envelope.payload
    return {
        "best_artifact": p.get("best_artifact", ""),
        "best_score": p.get("best_score", 0),
        "total_cycles": p.get("cycle", 0),
    }
