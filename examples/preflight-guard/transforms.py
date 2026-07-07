"""Preflight-guard transforms (app-side; the engine stays domain-free).

The pattern: a DETERMINISTIC transform validates and normalizes the request
BEFORE any model call. Garbage input is rejected early — you don't pay a model
call (latency + tokens) to discover the input was empty, missing a field, or
over a size limit. The guard writes `input_ok` = "yes" | "no" and the pipeline
`branch`es on it: "yes" -> the (paid) summarize agent, anything else -> reject.

Fail-closed: any input the guard can't positively clear routes to `reject`. The
branch's `default` is also `reject`, so a guard that returns an unexpected
`input_ok` value can never fall through to the model.

Targets Python 3.9+.
"""
from __future__ import annotations

import sys
from typing import Any, Dict


def guard(envelope, config) -> Dict[str, Any]:
    """Runs FIRST, before any model call. Validates + normalizes the raw request.

    Reads from payload:
      title (str)  — required, non-empty after strip
      text  (str)  — required, non-empty after strip, <= max_chars

    config.extras keys (set in the node's `config:` block):
      required  (list[str], default ["title", "text"]) — keys that must be present
      max_chars (int, default 280)                      — size guard on `text`

    Writes to payload:
      input_ok ("yes" | "no")  — the branch key
      error    (str)           — human-readable reason when input_ok == "no"
      title/text               — normalized (whitespace-stripped) on the pass path

    The guard NORMALIZES on the way through so downstream stages see clean input:
    validation and normalization are the same deterministic step, done once, for
    free, before the expensive stage.
    """
    p = envelope.payload
    extras = config.extras or {}
    required = extras.get("required", ["title", "text"])
    max_chars = int(extras.get("max_chars", 280))

    def _reject(reason: str) -> Dict[str, Any]:
        print("preflight: REJECT (no model call) — {}".format(reason), file=sys.stderr)
        return {**p, "input_ok": "no", "error": reason}

    for key in required:
        value = p.get(key)
        if not isinstance(value, str) or not value.strip():
            return _reject("missing or empty required field: {!r}".format(key))

    text = p["text"].strip()
    if len(text) > max_chars:
        return _reject("text too long: {} chars > limit {}".format(len(text), max_chars))

    title = p["title"].strip()
    print("preflight: OK — input cleared, proceeding to model", file=sys.stderr)
    return {**p, "input_ok": "yes", "error": "", "title": title, "text": text}


def reject(envelope, config) -> Dict[str, Any]:
    """Terminal reject path: NO model was called. Emits a clean error payload.

    In real use this is where you'd return an HTTP 400 to the caller, or route to
    a `human_gate` for a malformed-input queue. Kept as a plain terminal here so
    the example never touches a provider on the reject path.
    """
    p = envelope.payload
    error = p.get("error", "input rejected by preflight guard")
    print("preflight: refused without calling the model — {}".format(error), file=sys.stderr)
    return {"status": "rejected", "error": error}
