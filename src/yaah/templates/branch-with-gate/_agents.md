# Authoring rules for this yaah pipeline

Distilled from the yaah engine docs. These are the ~6 things that bite an AI
author or executor. Read once before editing the config.

## 1. An agent's reply REPLACES the payload

An `agent` node's reply becomes the *new* payload — it does NOT merge into the
old one. Only these survive across an agent step:

- `payload["raw"]` — the reply text,
- keys parsed out of that reply (see rule 2),
- keys named in the node's `carry:` list.

For loop-frame keys that must survive a *backward* edge, list them in
`graph.sticky`. Don't "clean up" `carry:` or `sticky` — drop a key and the
value it names vanishes, and a downstream `{{placeholder}}` fails the render
with `render_unfilled_placeholders`.

## 2. Agents parse JSON by default (ADR-0004)

An `agent` node parses its reply as JSON and merges the keys, so a reply of
`{"summary": "..."}` gives you `{{summary}}` downstream. You only add an
explicit `transform` parse step when you set `parse: false` on the node (e.g.
the reply is prose you want kept whole in `raw`).

## 3. `feedback` is engine-reserved — use `loop_feedback` for your own retries

The engine auto-writes `feedback` on a within-stage validator retry. If you
build your own verify/retry thread, use a DIFFERENT key (`loop_feedback`) so
you don't collide with the engine. And make sure the key your loop WRITES is
the key your prompt READS — a mismatch silently reads nothing.

A `strict_render: true` agent that reads its own loop key can stay strict-clean
by using `{{?loop_feedback}}` (the optional-placeholder sigil renders empty on
the first turn instead of faulting); see `yaah manual` → "Placeholders" for the
full `{{?key}}` / `{{!key}}` dialect.

## 4. Fence untrusted input with `{{!key}}`

A normal `{{key}}` interpolates trusted content. For anything a model produced
or a user supplied, use the fenced form `{{!key}}` so it can't smuggle
instructions into the prompt. Only an agent prompt fences — a `render` template
can't (`{{!key}}` there is a literal), so when a render's output feeds a
human/file and not a model prompt, set `allow_untrusted: true` on the render to
tell the `untrusted-unfenced` lint that unfenced agent text is legitimate there.

## 5. The authoring loop: edit → validate → fake → real

1. Edit the config.
2. `yaah validate --strict` — the gate for EVERY config change.
3. Run offline with the `.fake.json` / `fake_scripted` overlay (deterministic,
   no API calls) until it's green.
4. ONLY THEN point it at real models.

## 6. Gates: pick the right form, then drive the parked run

`form: "approve"` is a single button (approve only). For a real decision use
`form: "approve_or_revise"` or a `json_schema` form. To drive a parked run:

```
yaah list                       # find the parked baton
yaah baton-schema <run>         # see the decision form it expects
# write decision.json matching that schema
yaah resume <run> decision.json
```

---

Run `yaah manual` for offline docs — node types, root config keys, placeholders,
foreach, scripted-reply contract, repair loop (ships with the pip package; works
without the repo). Extended docs (`AGENTS.md`, `docs/archetypes.md`,
`docs/node-reference.md`) live in the yaah source repo and are NOT shipped with
the pip package.
