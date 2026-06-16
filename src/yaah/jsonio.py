"""extract_json — tolerant JSON extraction from LLM text.

Used by: the JsonObjectValidator and app transform nodes (e.g. a findings-merge
step) — anything that consumes an Agent's raw text and needs the JSON the model
was asked to produce.
Where: the seam between an opaque LLM worker (returns text) and a structured
consumer (wants an object).
Why: models routinely wrap JSON in ```json fences or add a sentence of prose
even when told "return ONLY JSON" (verified against real `claude -p`). A strict
json.loads then silently drops everything. This recovers the object without the
agent having to be non-opaque about its output.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
import re
from typing import Any

# A fenced block: ```json ... ``` or ``` ... ``` (language tag optional).
_FENCE = re.compile(r"```(?:[a-zA-Z0-9_-]+)?\s*\n?(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Parse the JSON value out of model text. Raises json.JSONDecodeError if none.

    Tries, in order: the whole string, the contents of any fenced code block,
    then the first balanced {...} or [...] span. The first that parses wins.
    """
    if not isinstance(text, str):
        raise json.JSONDecodeError("not a string", str(text), 0)

    candidates = [text.strip()]
    candidates += [m.group(1).strip() for m in _FENCE.finditer(text)]
    span = _first_balanced_span(text)
    if span is not None:
        candidates.append(span)

    last_err = None
    for cand in candidates:
        if not cand:
            continue
        try:
            return json.loads(cand)
        except json.JSONDecodeError as e:
            last_err = e
    raise last_err or json.JSONDecodeError("no JSON found", text, 0)


def _first_balanced_span(text: str) -> "str | None":
    """Return the first balanced {...} or [...] substring, ignoring braces in strings.

    Only `"` opens a JSON string — JSON has no single-quoted strings (RFC 8259).
    Treating `'` as a string delimiter was a quiet bug: model prose like
    `It's the result: {"a":1}` opened a fake string at the apostrophe, then
    consumed the real `{...}` as string content, then raised JSONDecodeError on
    the recovered "JSON". Only the double-quote convention is honored here."""
    opens = {"{": "}", "[": "]"}
    start = None
    stack = []
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch in opens:
            if start is None:
                start = i
            stack.append(opens[ch])
        elif ch in ("}", "]"):
            if stack and ch == stack[-1]:
                stack.pop()
                if not stack and start is not None:
                    return text[start : i + 1]
            else:
                start = None
                stack = []
    return None
