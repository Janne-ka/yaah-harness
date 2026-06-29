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
from abc import ABC, abstractmethod
from typing import Any

# A fenced block: ```json ... ``` or ``` ... ``` (language tag optional).
_FENCE = re.compile(r"```(?:[a-zA-Z0-9_-]+)?\s*\n?(.*?)```", re.DOTALL)

# Sentinel returned by a candidate-tier strategy to mean "not my shape, defer to the
# next strategy" — distinct from None, because `null` is a valid JSON value a strategy
# may legitimately return.
_DEFER = object()

def extract_json(text: str, keys: "list | None" = None, schema: "dict | None" = None) -> Any:
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
    or a nested object.

    `schema`: a stronger weak-executor backstop (Y5). When `keys`-recovery also fails AND
    BOTH a schema and a non-empty `keys` (the anchor) are given, the schema-gated
    key:value plucker (`UnquotedKeyValue`) runs. It
    recovers what Y4 cannot — unquoted bare VALUES (`{verdict: FIX}`), enum members and
    `type: string` free-form values, and the one-`key: value`-per-LINE shape — by reading
    each field against the schema (enum membership / declared type). All-or-nothing and
    never fabricates: an unknown key, an off-enum word, or a bare word for a numeric field
    fails the whole recovery. Accepted only if the result holds every `keys` entry.

    keys=None, schema=None (defaults) preserve the legacy ACCEPT/REJECT behaviour — the
    same inputs parse to the same value, the same inputs raise (the no-keys path scans only
    the FIRST balanced span, as legacy did, so a later valid object never silently rescues a
    malformed earlier one). The one deliberate difference: a truncated structure raises a
    clearer `truncated JSON: …` error instead of the raw json.loads message.

    Thin wrapper over the module-default `HardenedParser`, which is the single
    implementation that composes the candidate selection + strategy pipeline below.
    """
    return _HARDENED.parse(text, keys=keys, schema=schema)


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


class Scanner:
    """Case 2 — the string- and bracket-aware scanning PRIMITIVE.

    Shared, not a sibling: the orchestrator calls `balanced_spans` to find the
    top-level objects (#2 multi-object); the unquoted key:value plucker (#3) calls
    `split_top_level` to break fields and key/value apart. One depth counter, one
    string skipper, fully char-configurable — add a bracket pair / separator / quote
    and it's a one-entry change. A separator or bracket inside a string never counts.

    `brackets`: open->close map (default {}/[]/()). `separators`: default split chars.
    `quotes`: string delimiters (default `"` only — JSON has no single-quoted strings;
    the plucker constructs a Scanner with `'` added for Python/haiku-style values)."""

    def __init__(self, brackets=None, separators=(",",), quotes=('"',)):
        self._open = dict(brackets or {"{": "}", "[": "]", "(": ")"})
        self._close = {v: k for k, v in self._open.items()}
        self._seps = tuple(separators)
        self._quotes = tuple(quotes)

    def _walk(self, text: str):
        """Yield (index, char, depth, in_string) for each char, tracking bracket depth
        and string state. depth/in_string describe the position BEFORE the char acts,
        so a separator at depth 0 outside a string is a real top-level separator."""
        depth = 0
        in_str = False
        qc = ""
        esc = False
        for i, ch in enumerate(text):
            if in_str:
                yield i, ch, depth, True
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == qc:
                    in_str = False
                continue
            yield i, ch, depth, False
            if ch in self._quotes:
                in_str = True
                qc = ch
            elif ch in self._open:
                depth += 1
            elif ch in self._close:
                depth = max(0, depth - 1)

    def match(self, text: str, open_idx: int) -> "int | None":
        """Index of the close bracket matching the opener at `open_idx`, or None if it
        never closes (truncated input)."""
        depth = 0
        for i, ch, _, in_str in self._walk(text[open_idx:]):
            if in_str:
                continue
            if ch in self._open:
                depth += 1
            elif ch in self._close:
                depth -= 1
                if depth == 0:
                    return open_idx + i
        return None

    def balanced_spans(self, text: str) -> list:
        """Every top-level (depth-0) balanced bracket substring, in order. Braces in
        strings are ignored; nested brackets stay inside their one span."""
        spans = []
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            if ch in self._quotes:                 # skip a top-level string whole
                end = self._skip_string(text, i)
                i = end
                continue
            if ch in self._open:
                close = self.match(text, i)
                if close is None:
                    break                          # truncated -> no more spans
                spans.append(text[i:close + 1])
                i = close + 1
                continue
            i += 1
        return spans

    def _skip_string(self, text: str, start: int) -> int:
        """Index just past the string that opens at `start`."""
        qc = text[start]
        i = start + 1
        n = len(text)
        while i < n:
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == qc:
                return i + 1
            i += 1
        return n

    def split_top_level(self, text: str, separators=None, maxsplit: int = -1) -> list:
        """Split `text` at any of `separators` that sit at depth 0 outside a string.
        Brackets and strings are honored, so `[1,2]` or `"a,b"` are never cut. Parts
        are stripped and empties dropped (consecutive separators collapse). `maxsplit`
        caps the number of splits (-1 = unlimited) so a key:value pluck stops after the
        first `:` and keeps `http://x` intact in the value."""
        seps = set(self._seps if separators is None else separators)
        parts = []
        buf = []
        n = 0
        for _, ch, depth, in_str in self._walk(text):
            if (not in_str and depth == 0 and ch in seps
                    and (maxsplit < 0 or n < maxsplit)):
                parts.append("".join(buf))
                buf = []
                n += 1
            else:
                buf.append(ch)
        parts.append("".join(buf))
        return [p.strip() for p in parts if p.strip()]


class BareValueResolver:
    """Case 3 ∩ schema — decide whether an unquoted bare value is safe to accept.

    A bare token is accepted only when the schema makes its meaning unambiguous:
      - the field is an enum and the token is a permitted member, OR
      - the field is declared `type: string` — the delimited rest-of-line IS the
        string value (reading a KNOWN field's value, not fabricating from prose;
        this is what a `reason: <free text>` line needs).
    Everything else is rejected: an unknown field, or a typed-non-string field
    (number/integer/boolean) handed a bare word — so a numeric field never silently
    swallows a stray word. Injected into the plucker; subclass + inject to swap."""

    def resolve(self, field: str, word: str, schema) -> tuple:
        """Return (ok, value). ok is True only for a schema-sanctioned bare value."""
        spec = ((schema or {}).get("properties") or {}).get(field) or {}
        if "enum" in spec:
            return (True, word) if word in spec["enum"] else (False, None)
        if spec.get("type") == "string":
            return True, word
        return False, None


class ParseStrategy(ABC):
    """A single tolerant-parse strategy. `parse` returns the recovered object, or
    None to DEFER to the next strategy (never a wrong guess). Subclass + inject
    dependencies for reuse; the HardenedParser runs the subclasses in order."""

    @abstractmethod
    def parse(self, candidate: str, *, schema=None) -> Any:
        ...


class UnquotedKeyValue(ParseStrategy):
    """Case 3 — the bare `verdict: FIX, confidence: high` shape (keys and/or values
    unquoted), AND the line-protocol shape (one `key: value` per line). Composes an
    injected `Scanner` (field + key:value splitting, bracket- and string-aware) and an
    injected `BareValueResolver` (gate bare-word values). All-or-nothing: any field
    that can't be safely resolved -> None, so the caller falls through rather than
    accept a partial/fabricated object. Separators + the depth cap are constructor
    params (`field_seps`, `kv_seps`, `line_sep`, `max_depth`) — extend = one entry.

    NESTED recovery: a bare value the schema declares a nested object/array is recovered
    RECURSIVELY (schema-guided), up to `max_depth` OBJECT levels (default 2; an array is
    a transparent container and does not cost a level). The SAME gate applies at every
    level, so a bare value deep in a `findings[]` element is only accepted if its nested
    schema makes it an enum member / `type:string` — nothing is fabricated, and one bad
    field/element fails the whole object/array.

    Field boundary: NEWLINE when the text contains one (line protocol — a comma is
    then part of a free-form value), else COMMA (inline `{a: 1, b: 2}` objects, whose
    values are short scalars). The key:value split is bounded to the first `:` so a
    value may itself contain a colon (`reason: use for x in xs:`). A single trailing
    field comma is tolerated.

    Known limitations (documented, not yet needed — gather evidence before building):
      - nesting beyond `max_depth` object levels is not recovered (cheap models rarely
        emit reliable deeper nesting anyway; raise `max_depth` if a stage needs it);
      - a value spanning MULTIPLE lines is split as separate fields and rejected
        (the line protocol is one pair per line);
      - a known property with neither `enum` nor `type: string` rejects a bare word
        (conservative: never coerces a word into a number/bool — see BareValueResolver)."""

    def __init__(self, scanner=None, resolver=None,
                 field_seps=(",",), kv_seps=(":",), line_sep="\n", max_depth=2):
        # default scanner honors BOTH quote styles so a single-quoted haiku value
        # ('looks risky') is treated as one string, not split on its inner spaces.
        self._scanner = scanner or Scanner(quotes=('"', "'"))
        self._resolver = resolver or BareValueResolver()
        self._field_seps = tuple(field_seps)
        self._kv_seps = tuple(kv_seps)
        self._line_sep = line_sep
        self._max_depth = max_depth

    def parse(self, candidate: str, *, schema=None) -> Any:
        return self._pluck_object(candidate, schema or {}, 0)

    def _pluck_object(self, text: str, schema, depth: int) -> Any:
        """Pluck one object's fields against `schema`'s `properties`. A field whose value
        is a bare nested object/array is recovered recursively (schema-guided), up to
        `max_depth` OBJECT levels — an array is a transparent container, it does not cost
        a level. The same no-fabrication gate applies at every level. All-or-nothing:
        any field that can't be resolved -> None."""
        text = text.strip()
        if text.startswith("{"):                 # unwrap one outer object pair
            close = self._scanner.match(text, 0)
            if close == len(text) - 1:
                text = text[1:close]
        # one field per LINE when newline-delimited (the line protocol — a comma is
        # then part of a free-form value, never a field break); else comma-delimited
        # (the inline `{a: 1, b: 2}` object shape, whose values are short scalars).
        seps = (self._line_sep,) if self._line_sep in text else self._field_seps
        fields = self._scanner.split_top_level(text, seps)
        if not fields:
            return None
        props = (schema.get("properties") or {})
        out = {}
        for field in fields:
            kv = self._scanner.split_top_level(field, self._kv_seps, maxsplit=1)
            if len(kv) != 2:
                return None                       # not a key:value field
            key = self._unquote(kv[0])
            raw = kv[1].strip()
            if raw.endswith(","):                 # tolerate a trailing field comma
                raw = raw[:-1].strip()
            ok, value = self._coerce_value(key, raw, props.get(key) or {}, depth)
            if not ok:
                return None
            out[key] = value
        return out or None

    def _pluck_array(self, text: str, items: dict, depth: int) -> Any:
        """Recover a bare array's elements against the `items` schema (SAME depth — the
        array is a transparent container). All-or-nothing: any bad element -> None."""
        text = text.strip()
        if text.startswith("["):
            close = self._scanner.match(text, 0)
            if close == len(text) - 1:
                text = text[1:close]
        elements = self._scanner.split_top_level(text, (",", self._line_sep))
        if not elements:
            return None
        out = []
        for element in elements:
            ok, value = self._coerce_value("", element, items, depth)
            if not ok:
                return None
            out.append(value)
        return out

    @staticmethod
    def _unquote(token: str) -> str:
        token = token.strip()
        if len(token) >= 2 and token[0] in "\"'" and token[-1] == token[0]:
            return token[1:-1]
        return token

    def _coerce_value(self, key: str, raw: str, spec: dict, depth: int) -> tuple:
        """A value is safe if it parses as a literal (number/bool/null/quoted string, or
        an already-valid nested object/array); else, when the schema declares it a nested
        object/array and we are under `max_depth`, it is recovered RECURSIVELY (the same
        gate applies at every level, so nothing is fabricated); else it is an enum member
        or `type:string` bare word. Anything else -> (False, None)."""
        raw = raw.strip()
        for parse in (json.loads, ast.literal_eval):
            try:
                value = parse(raw)
            except (ValueError, SyntaxError):
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                return True, value
            if isinstance(value, (dict, list)):
                try:
                    json.dumps(value)
                    return True, value
                except TypeError:
                    return False, None
        if depth < self._max_depth:
            kind = spec.get("type")
            if kind == "object" and raw.startswith("{"):
                nested = self._pluck_object(raw, spec, depth + 1)
                if nested is not None:
                    return True, nested
            if kind == "array" and raw.startswith("["):
                arr = self._pluck_array(raw, spec.get("items") or {}, depth)
                if arr is not None:
                    return True, arr
        return self._resolver.resolve(key, raw, {"properties": {key: spec}})


class DecoyKeyDetector:
    """Case 1 — tell a prompt's format-EXAMPLE object apart from the model's real answer.

    A leading example like `look like: {"verdict":"PASS"}` shares the answer's keys,
    so a span scan can grab the example by mistake (silent wrong data). The fix:
    examples declare DECOY keys marked by a recognizable affix — real key `verdict`
    becomes `n_o_verdict_o_n` in the example — and the parser skips any object whose
    keys carry the marker. Detection is by configurable prefix and/or suffix; adding a
    marker is a one-line change to `prefixes`/`suffixes`. Empty affixes are ignored."""

    def __init__(self, prefixes=("n_o_",), suffixes=("_o_n",)):
        self._prefixes = tuple(p for p in prefixes if p)
        self._suffixes = tuple(s for s in suffixes if s)

    def is_decoy(self, key: str) -> bool:
        """A key is a decoy if it carries a marker prefix OR suffix."""
        return (any(key.startswith(p) for p in self._prefixes)
                or any(key.endswith(s) for s in self._suffixes))

    def is_decoy_object(self, obj: Any) -> bool:
        """An object is a format example (skip it) if it's a non-empty dict whose
        keys carry the decoy marker. A real answer never carries the marker."""
        return isinstance(obj, dict) and bool(obj) and all(
            self.is_decoy(k) for k in obj)


class PureJson(ParseStrategy):
    """Tier 1 — strict `json.loads`. Returns any JSON value (object/array/scalar/null),
    or `_DEFER` when the candidate is not valid JSON."""

    def parse(self, candidate: str, *, schema=None) -> Any:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return _DEFER


class KeyValueRepair(ParseStrategy):
    """Tier 2 — a weak executor's Python-dict / single-quoted style (`{'k': 'v'}`,
    `True/None`, trailing commas). `ast.literal_eval` is SAFE (literals only, never
    code). Accepts ONLY a dict/list (we want the object, not a stray quoted scalar)
    that is JSON-serialisable — a Python-only shape (tuple keys, set/bytes values) is
    rejected so it can't raise a later TypeError when the payload is json.dumps'd."""

    def parse(self, candidate: str, *, schema=None) -> Any:
        try:
            obj = ast.literal_eval(candidate)
        except (ValueError, SyntaxError):
            return _DEFER
        if not isinstance(obj, (dict, list)):
            return _DEFER
        try:
            json.dumps(obj)
        except TypeError:
            return _DEFER
        return obj


class HardenedParser:
    """The single tolerant-parse implementation `extract_json` delegates to. Composes
    the pieces built around it instead of inlining their logic:

      candidates  = whole string + fenced blocks + EVERY top-level balanced span
                    (`Scanner.balanced_spans`, not just the first — that is what lets
                    the ambiguity guard see a format-example sitting beside the answer)
      decoy filter = drop a candidate that is a marked format example (`DecoyKeyDetector`)
      strategies   = [PureJson, KeyValueRepair] tried tier-by-tier across candidates
      ambiguity    = with `keys`, if >1 surviving candidate parses to a DISTINCT object
                     holding all keys -> RAISE rather than silently pick the first
                     (kills the silent-wrong-data bug)
      recovery     = Y4 key-guided repair, then Y5 schema-gated plucking (anchored on
                     `keys`, all-or-nothing — see extract_json's docstring)
      truncation   = if nothing parses and the first JSON opener never closes, the raised
                     error says so (a clearer signal for a retry backstop)

    Strategies/scanner/decoy are constructor-injected, so the pipeline is swappable and
    extendable. Stateless across calls — `keys`/`schema` are per-call parse arguments."""

    def __init__(self, strategies=None, scanner=None, decoy=None):
        # JSON brackets only for candidate scanning ('()' is not a JSON container);
        # double-quote strings only (RFC 8259 — an apostrophe in prose is not a string).
        self._scanner = scanner or Scanner(brackets={"{": "}", "[": "]"}, quotes=('"',))
        self._decoy = decoy or DecoyKeyDetector()
        self._strategies = list(strategies) if strategies is not None else [
            PureJson(), KeyValueRepair()]

    def parse(self, text: str, keys=None, schema=None) -> Any:
        if not isinstance(text, str):
            raise json.JSONDecodeError("not a string", str(text), 0)
        candidates = self._candidates(text, keys)
        # tier by tier (all strict-JSON first, then all literal-eval) so a valid-JSON
        # candidate anywhere beats a loose parse anywhere — preserves legacy precedence.
        for strategy in self._strategies:
            selected = self._select(strategy, candidates, keys, text)
            if selected is not _DEFER:
                return selected
        # Y4: bounded key-guided recovery (opt-in via `keys`).
        if keys:
            recovered = _recover_by_keys(text, list(keys))
            if recovered is not None:
                return recovered
        # Y5: schema-gated key:value plucking, anchored on `keys`, all-or-nothing.
        if schema is not None and keys:
            plucked = UnquotedKeyValue().parse(text, schema=schema)
            if isinstance(plucked, dict) and all(k in plucked for k in keys):
                return plucked
        raise self._failure(text)

    def _candidates(self, text: str, keys) -> list:
        cands = [text.strip()]
        cands += [m.group(1).strip() for m in _FENCE.finditer(text)]
        # With `keys`, scan EVERY top-level span so the ambiguity guard can see a format
        # example sitting beside the real answer. WITHOUT keys there is no anchor and no
        # guard, so restrict to the FIRST balanced span — exactly the legacy parser — so a
        # later valid object can never silently rescue a malformed earlier one (the
        # unguarded json_object path must stay fail-loud).
        if keys:
            cands += self._scanner.balanced_spans(text)
        else:
            span = _first_balanced_span(text)
            if span is not None:
                cands.append(span)
        return [c for c in cands if c]

    def _select(self, strategy, candidates, keys, text):
        """Run one strategy across the candidates. Drop decoy (format-example) objects.
        Returns the chosen value, or `_DEFER` if this strategy matched nothing. With
        `keys`: if more than one candidate parses to a DISTINCT object holding every key,
        raise (ambiguous); otherwise prefer the single qualifying object, else the first
        parse (legacy: downstream checks keys)."""
        parsed = []
        for cand in candidates:
            obj = strategy.parse(cand)
            if obj is _DEFER or self._decoy.is_decoy_object(obj):
                continue
            parsed.append(obj)
        if not parsed:
            return _DEFER
        if keys:
            qualifying = [o for o in parsed
                          if isinstance(o, dict) and all(k in o for k in keys)]
            distinct = {self._fingerprint(o) for o in qualifying}
            if len(distinct) > 1:
                raise json.JSONDecodeError(
                    "ambiguous JSON: {} top-level objects match the required keys {} "
                    "(a format example beside the answer?) — refusing to guess".format(
                        len(distinct), list(keys)), text, 0)
            if qualifying:
                return qualifying[0]
        return parsed[0]

    @staticmethod
    def _fingerprint(obj) -> str:
        return json.dumps(obj, sort_keys=True, default=str)

    def _failure(self, text: str) -> json.JSONDecodeError:
        """A clearer error when the input is a TRUNCATED structure (first JSON opener
        never closes) vs simply containing no JSON — a retry backstop can act on it."""
        for i, ch in enumerate(text):
            if ch in "{[":
                if self._scanner.match(text, i) is None:
                    return json.JSONDecodeError(
                        "truncated JSON: {!r} at index {} is never closed".format(ch, i),
                        text, i)
                break
        return json.JSONDecodeError("no JSON found", text, 0)


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


# The module-default parser `extract_json` delegates to. Stateless across calls, so a
# single shared instance is safe; construct a `HardenedParser(...)` with custom
# strategies/scanner/decoy to vary the pipeline.
_HARDENED = HardenedParser()
