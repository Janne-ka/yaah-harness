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

import ast
import json
import re
from typing import Any

# A fenced block: ```json ... ``` or ``` ... ``` (language tag optional).
_FENCE = re.compile(r"```(?:[a-zA-Z0-9_-]+)?\s*\n?(.*?)```", re.DOTALL)

def extract_json(text: str, keys: "list | None" = None) -> Any:
    """Parse the JSON value out of model text. Raises json.JSONDecodeError if none.

    Tries, in order: the whole string, the contents of any fenced code block,
    then the first balanced {...} or [...] span. The first that parses wins.

    `keys`: a weak-executor backstop (Y4). When all of the above fail AND `keys` is a
    non-empty list of EXPECTED output keys (e.g. a json_schema's `required`), repair the
    first balanced {...} span by quoting its bare object keys — STRING-AWARELY, so a
    `key:` sequence inside a quoted value or a nested object is never rewritten — then
    parse, accepting only a dict that contains every expected key at TOP level.
    Recovers a weak executor's habitual unquoted-key shape (`{verdict:"FIX"}`) WITHOUT a
    permissive transform that could corrupt valid JSON or fabricate a value from prose
    or a nested object. Unquoted bare-word VALUES are not recovered (an unsafe guess).
    keys=None (default) is byte-identical to the legacy behaviour.
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

    # Last resort: a weak executor emitted Python-dict / single-quoted style
    # (`{'k': 'v'}`) — strict JSON rejects the single quotes, but the object is
    # recoverable. ast.literal_eval is SAFE (literals only, never code) and
    # handles single-quoted strings + Python True/False/None + trailing commas.
    # Accept ONLY a dict/list (we want the object, not a stray quoted scalar).
    # Deliberately does NOT cover JS `true/false/null` or unquoted keys (both
    # invalid Python too) — those still raise below.
    for cand in candidates:
        if not cand:
            continue
        try:
            obj = ast.literal_eval(cand)
        except (ValueError, SyntaxError):
            continue
        if isinstance(obj, (dict, list)):
            try:
                # Reject Python-only shapes JSON can't represent (tuple keys,
                # set/bytes values) so they fall into the clean JSONDecodeError
                # retry path here, not an uncaught TypeError when the payload is
                # later json.dumps'd (trace / NATS).
                json.dumps(obj)
            except TypeError:
                continue
            return obj

    # Y4 weak-executor backstop: bounded key-guided recovery (opt-in via `keys`).
    if keys:
        recovered = _recover_by_keys(text, list(keys))
        if recovered is not None:
            return recovered
    raise last_err or json.JSONDecodeError("no JSON found", text, 0)


def _recover_by_keys(text: str, keys: list) -> "dict | None":
    """Recover the object from the first balanced {...} span when normal parsing failed.
    Quotes the span's bare object keys (string-aware — never touching a `key:` inside a
    quoted value or nested object, the masking bug a naive regex caused) and re-parses.
    Accept ONLY a dict that holds every expected key at TOP level; otherwise None, so the
    caller still raises rather than accept a fabricated/partial/wrong value. Unquoted
    bare-word VALUES are not recovered (an unsafe guess)."""
    span = _first_balanced_span(text)
    if span is None:
        return None
    repaired = _quote_bare_keys(span)
    for parse in (json.loads, ast.literal_eval):
        try:
            obj = parse(repaired)
        except (ValueError, SyntaxError):
            continue
        if not (isinstance(obj, dict) and all(k in obj for k in keys)):
            continue
        try:
            json.dumps(obj)  # reject Python-only shapes (tuple keys, set/bytes values)
        except TypeError:
            continue
        return obj
    return None


def _quote_bare_keys(s: str) -> str:
    """Wrap bare (unquoted) object keys in double quotes, tracking string state so a
    `name:` sequence inside a quoted value is NEVER rewritten. A key is an identifier
    sitting right after `{` or `,` (modulo whitespace) and immediately before `:`, at
    any nesting level. This is the safe core of recovery — it repairs the one weak-
    executor habit (unquoted keys) without corrupting string contents."""
    out = []
    i, n = 0, len(s)
    in_str = False
    qc = ""
    while i < n:
        c = s[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(s[i + 1]); i += 2; continue
            if c == qc:
                in_str = False
            i += 1; continue
        if c == '"' or c == "'":
            in_str = True; qc = c; out.append(c); i += 1; continue
        if c == "{" or c == ",":
            out.append(c); i += 1
            while i < n and s[i] in " \t\r\n":
                out.append(s[i]); i += 1
            if i < n and (s[i].isalpha() or s[i] == "_"):
                j = i
                while j < n and (s[j].isalnum() or s[j] in "_-"):
                    j += 1
                k = j
                while k < n and s[k] in " \t\r\n":
                    k += 1
                if k < n and s[k] == ":":
                    out.append('"' + s[i:j] + '"')
                    i = j
            continue
        out.append(c); i += 1
    return "".join(out)


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
