# JSON recovery for weak executors (Y4 + Y5)

A weak executor (haiku/sonnet) often returns *almost*-JSON: fenced blocks, prose around
the object, unquoted keys, bare/unquoted values, or one `key: value` per line instead of
an object. `yaah.jsonio.extract_json` recovers these **without fabricating** — but the
strongest recovery (Y5) only switches on when your stage **declares an `output_schema`**.
This is the one thing to get right so a `parse:true` stage Just Works on haiku.

## TL;DR for authoring a `parse:true` stage

Declare an `output_schema` with **`required`** and **typed `properties`**:

```jsonc
{
  "type": "agent",
  "parse": true,
  "output_schema": {
    "required": ["verdict", "severity", "confidence", "reason"],
    "properties": {
      "verdict":    { "enum": ["FIX", "SKIP", "ESCALATE"] },
      "severity":   { "enum": ["high", "medium", "low"] },
      "confidence": { "type": "integer" },
      "reason":     { "type": "string" }
    }
  }
}
```

With that in place, every shape in the table below is recovered to the same object.
Omit `output_schema` (or its `required`) and you fall back to Y4 (weaker — see below).

## What `extract_json` accepts, tier by tier

| Tier | Fires when | Recovers |
|---|---|---|
| 1 — strict | always | clean JSON, ```json fenced``` JSON, JSON embedded in prose |
| 2 — literal | tier 1 fails | single-quoted / Python-literal objects (`{'k': 'v'}`, `True/None`, trailing commas) |
| Y4 — keys | `keys` (required) given | unquoted **keys** with quoted values: `{verdict:"FIX"}` |
| **Y5 — schema** | **`output_schema` with `required`** | unquoted **values**, enum members, `type:string` free-form, **one `key: value` per line** |

Y5 is what handles real haiku output like:

```
verdict: FIX
severity: high
confidence: 100
reason: SQL injection — user_id concatenated directly into the query string
```

…and malformed-JSON shapes Y4 can't, e.g. `{verdict: FIX, confidence: 90, reason: "x"}`.

## The contract (why it is safe to leave on)

- **Anchored** — Y5 recovery requires `required` keys; with none declared it does not run
  (nothing to verify against). No required key ⇒ no Y5.
- **All-or-nothing** — every `required` key must be recovered, or the whole thing raises
  `not_json` (the harness's retry/feedback loop then fires). A half-populated object never
  flows downstream.
- **Never fabricates** — a value is accepted only when the schema makes it unambiguous:
  an `enum` member, a `type: string` (the rest of the line *is* the string), or a literal
  (number/bool/quoted). An off-enum word (`verdict: MAYBE`), a bare word for a numeric
  field (`confidence: lots`), or an unknown key all FAIL rather than guess.
- **Backward compatible** — `extract_json(text)` with no `keys`/`schema` preserves the
  legacy accept/reject behaviour: the same inputs parse, the same inputs raise (the no-keys
  path scans only the first balanced span, so a later valid object never silently rescues a
  malformed earlier one). The lone deliberate difference is a clearer error on truncation.

## Field separation

One field per **line** when the output contains newlines (the line shape — a comma is then
part of a free-form value), else comma-separated (inline `{a: 1, b: 2}`). The key:value
split is bounded to the first `:`, so a value may itself contain a colon. A single trailing
field comma is tolerated.

## Safety guards (HardenedParser)

`extract_json` delegates to `HardenedParser`, which adds three guards over the plain tier
list — relevant when you author prompts:

- **Ambiguity guard.** If the model emits more than one top-level object that holds your
  `required` keys (e.g. it echoed a format *example* next to its real answer), the parser
  **refuses to guess** and raises `not_json` (retry fires) rather than silently taking the
  first. So: don't show an example object with the *same* keys in the prompt unless you
  mark it (below).
- **Decoy filter.** Mark the keys of any example object with the `n_o_…_o_n` affix
  (`{"n_o_verdict_o_n": "PASS"}`); the parser drops marked objects, so your real answer
  wins even when the model echoes the example.
- **Truncation signal.** Output cut at `max_tokens` (first `{`/`[` never closes) raises a
  `truncated JSON: …` error — a clear signal for a retry/escalate backstop.

## Known limitations (by design — measure before extending)

- A value spanning **multiple lines** is treated as separate fields and rejected (the line
  shape is one pair per line).
- A declared property with **neither `enum` nor `type: string`** rejects a bare word
  (it will not coerce a word into a number/boolean) — give numeric fields `type: integer`
  / `type: number` and they recover from a numeric literal.

## Where this lives

`src/yaah/jsonio.py` — `extract_json` (the entry point) delegates to `HardenedParser`
(the orchestrator), which composes: `PureJson` + `KeyValueRepair` (the candidate-tier
strategies), `Scanner` (bracket/string-aware splitting), `DecoyKeyDetector` (the decoy
filter), `UnquotedKeyValue` (the Y5 plucker), `BareValueResolver` (the schema gate). All
are constructor-injected into `HardenedParser`, so the pipeline is swappable. Tests:
`tests/test_hardened_parser.py` (the guards), `tests/test_jsonio_schema.py` (Y5 wiring),
`tests/test_parsers.py` (the pieces), `tests/test_jsonio_keys.py` (Y4),
`tests/test_jsonio.py` (the tolerant base).
