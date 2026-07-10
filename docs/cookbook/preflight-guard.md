# Preflight guard — validate input before you pay for a model call

Copy this pattern when a stage that costs money or latency (an agent, an
`agent_loop`, a fork of lenses) sits behind input you don't control, and a large
fraction of that input can be rejected **deterministically** — missing a required
field, empty, over a size limit, wrong shape. Do the cheap check first; only clean
input reaches the model.

**Reference implementation:** [`examples/preflight-guard/`](../../examples/preflight-guard/)

---

## The shape

```
guard (transform: required-field + size/shape check, normalize → input_ok)
   ↓ branch on input_ok
   ├─ "yes"       → work    (the paid stage: agent / agent_loop / fork)
   └─ "no"/default → reject  (transform: clear error, NO model call)
```

The guard is a `transform`, not an agent — it runs **before** any provider is
touched. It writes a plain string branch key and the `branch` routes on it. There is
no new engine construct: a transform, a branch, and a route map you already have.

---

## The config skeleton

```json
"nodes": {
  "role:guard": {
    "type": "transform", "call": "envelope",
    "target": "fn:transforms:guard",
    "config": {"required": ["title", "text"], "max_chars": 280},
    "provides": ["input_ok", "error", "title", "text"]
  },
  "role:work":   {"type": "agent", "prompt": "file:work", "model": "claude:claude-haiku-4-5",
                  "output_schema": {"properties": {"summary": {"type": "string"}}, "required": ["summary"]}},
  "role:reject": {"type": "transform", "call": "envelope", "target": "fn:transforms:reject",
                  "provides": ["status", "error"]}
},
"graph": {
  "start": "guard",
  "stages": {
    "guard":  {"node": "role:guard",
               "branch": {"on": "input_ok", "routes": {"yes": "work"}, "default": "reject"}},
    "work":   {"node": "role:work",   "then": null, "final": true},
    "reject": {"node": "role:reject", "then": null, "final": true}
  }
}
```

## The guard transform

```python
def guard(envelope, config):
    p = envelope.payload
    extras = config.extras or {}
    required  = extras.get("required", ["title", "text"])
    max_chars = int(extras.get("max_chars", 280))

    def _reject(reason):
        return {**p, "input_ok": "no", "error": reason}

    for key in required:                       # required-field + non-empty check
        value = p.get(key)
        if not isinstance(value, str) or not value.strip():
            return _reject("missing or empty required field: {!r}".format(key))

    text = p["text"].strip()
    if len(text) > max_chars:                  # size guard
        return _reject("text too long: {} chars > limit {}".format(len(text), max_chars))

    # PASS: normalize on the way through so the model sees clean input.
    return {**p, "input_ok": "yes", "error": "", "title": p["title"].strip(), "text": text}
```

Validation and normalization are the **same** deterministic step — done once, for
free, before the expensive stage.

---

## Why this belongs in a transform + branch, not in the agent

An agent *can* reject bad input — but only after you've paid for the call and waited
for the reply, and a weak model may cheerfully summarize garbage instead of refusing.
A transform check is **free, instant, and total**: it runs every time, costs no
tokens, and its verdict is a deterministic function of the payload, not a model's mood.

Reach for a preflight guard when:
- A meaningful fraction of real input is rejectable on **shape alone** (present,
  non-empty, within size, parses).
- The expensive stage has real per-call cost (tokens, latency, a rate limit).
- You want the rejection reason to be exact and machine-stable (`error` string),
  not a paragraph a model wrote.

Don't reach for it when the only thing separating good input from bad is **meaning**
(on-topic? sincere? adversarial prose?) — that judgement needs a model. A cheap-model
*pattern* check ([`tool-safety-gate/`](../../examples/tool-safety-gate/)) sits between
the two: still one model call, but competent at recognizing a *shape* a regex can't.

---

## Fail-closed wiring

Route the **positive** branch explicitly and make `default` the reject side:

```json
"branch": {"on": "input_ok", "routes": {"yes": "work"}, "default": "reject"}
```

If the guard ever returns an unexpected `input_ok` value (a refactor bug, a new code
path that forgets to set it), the run routes to `reject`, not to the paid stage. Never
put the expensive stage on `default`.

---

## Honest limits

**A shape gate, not a semantic one.** The guard proves the input is *well-formed*, not
*sensible*. Well-formed nonsense, off-topic-but-valid, and prompt-injection prose all
pass a shape check. Layer a model check on top when meaning matters; the guard's job is
only to kill the obvious-garbage class before it reaches one.

**The guard is a `transform` — its return value REPLACES the payload.** Return
`{**envelope.payload, ...}`, not just your new keys, or every input field the work
stage reads (`title`, `text`) vanishes before the model sees it. This is the data-flow
contract that bites every author (see [AGENTS.md](../../AGENTS.md) "Authoring a
pipeline"). Declare the surviving keys in `provides:` so the author-time lint can see
them.

**`max_chars` is a character count, not a token budget.** It's a coarse cost guard,
not an exact one — a 280-character limit is roughly 70 tokens of English but varies by
language and content. If you need a hard token ceiling, count tokens in the guard
(with your provider's tokenizer) instead of characters, and document the model it's
calibrated for.

---

## Checklist for adapting this recipe

1. List the keys the expensive stage actually **reads**, and put them in the guard's
   `required` and in `provides:`.
2. Pick the cheapest checks that catch the most real garbage (present, non-empty,
   size, JSON-parses) — don't reimplement the model's job.
3. Return `{**envelope.payload, ...}` from the guard so downstream keys survive.
4. Wire the branch fail-closed: name the pass route, make `default` the reject side.
5. Give `reject` a machine-stable `error`; in production it becomes your 400 / your
   malformed-input `human_gate` queue.
6. Verify offline with a fake provider and a rejectable default input — confirm the
   reject run's trace shows **no** model-call stage.
