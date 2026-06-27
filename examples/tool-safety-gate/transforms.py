"""Tool-safety-gate transforms (app-side; the engine stays domain-free).

The pattern: a CHEAP model (haiku) emits ONE keyword (SAFE/UNSAFE) — never JSON —
so the BUG-697 failure mode (haiku reliably mangles JSON: unquoted keys, empty
output) cannot break it. `extract_verdict` pulls the keyword out of the raw reply
and the pipeline `branch`es on it. Fail-safe: any reply that isn't a clean
SAFE/UNSAFE (empty, garbled, hedging) becomes BLOCK and routes away from execution.
"""
from __future__ import annotations

import re
import sys
from typing import Any, Dict


def extract_verdict(envelope, config) -> Dict[str, Any]:
    """Runs right after the haiku safety-check agent (parse:false → the reply text
    is in `raw`, NOT parsed). Writes `safety` = SAFE | UNSAFE | BLOCK for the next
    stage's branch. The rule is ASYMMETRIC by design — liberal on UNSAFE, strict on
    SAFE — so the gate fails toward refusing:
      - UNSAFE: any mention of the word anywhere in the reply (checked FIRST — the
        word UNSAFE contains SAFE).
      - SAFE: ONLY a clean lone token (the whole reply is just `SAFE`, ignoring
        surrounding punctuation/whitespace). A hedging reply that merely CONTAINS
        the word safe ("probably safe, but be careful") must NOT approve.
      - anything else (empty, garbled, hedged, prose) → BLOCK.
    So a malformed OR a hedged cheap-model reply NEVER auto-approves — that fail-safe
    is the whole point of the gate."""
    raw = (envelope.payload.get("raw") or "").strip()
    if re.search(r"\bUNSAFE\b", raw, re.IGNORECASE):
        verdict = "UNSAFE"
    elif re.fullmatch(r"[\W_]*safe[\W_]*", raw, re.IGNORECASE):  # lone SAFE only
        verdict = "SAFE"
    else:
        verdict = "BLOCK"  # empty / garbled / hedged / prose-with-"safe" → fail safe
    print("safety-gate: cheap model said {!r} -> {}".format(raw.strip()[:48], verdict),
          file=sys.stderr)
    return {**envelope.payload, "safety": verdict}


def approve(envelope, config) -> Dict[str, Any]:
    """SAFE route: STAND-IN for the real destructive node. In real use, replace
    this stage with the actual action (a `shell` node, a tool call) — the gate has
    cleared it. Kept as a no-op here so the example never executes anything."""
    print("APPROVED — would execute: {}".format(envelope.payload.get("command", "")),
          file=sys.stderr)
    return {**envelope.payload, "result": "approved"}


def block(envelope, config) -> Dict[str, Any]:
    """UNSAFE / BLOCK route: the destructive node is NOT reached. In real use this
    would route to a `human_gate` to escalate; here it terminates with a result."""
    print("BLOCKED — refused to execute: {}".format(envelope.payload.get("command", "")),
          file=sys.stderr)
    return {**envelope.payload, "result": "blocked"}
