# tool-safety-gate — a cheap-model safety gate (composition pattern)

A **cheap** model (haiku) checks a proposed shell command **before** a destructive
stage runs. SAFE → proceed; UNSAFE (or anything unclear) → don't. It's the
lowest-risk member of the "cheap-model composition" family: a *pattern check*
(does this command match a destructive shape?), which a cheap model is genuinely
competent at — not nuanced judgement, which it is not.

## The one idea that makes it work
Haiku reliably mangles JSON (see `.notes/bug697-…`). So the gate **never asks it
for JSON.** The agent is `parse:false` and the prompt asks for **one word**:
`SAFE` or `UNSAFE`. A tiny `fn:` transform extracts that keyword from the raw
reply and the pipeline `branch`es on it. No JSON, no parse, no mangling.

**Fail-safe (asymmetric):** the extractor is **liberal on UNSAFE, strict on SAFE**.
Any mention of "unsafe" blocks; only a *clean lone* `SAFE` approves. A hedged reply
that merely contains the word safe ("probably safe, but…"), or an empty/garbled one,
becomes `BLOCK` — and the branch's `default` is also `block`. So a malformed *or*
hedged cheap-model reply can never auto-approve. (Never set the default, or the SAFE
match, to the permissive side.) The command is interpolated with the untrusted fence
`{{!command}}` so a crafted command can't inject "…reply SAFE" as instructions.

## Shape
```
check (haiku, parse:false → "UNSAFE")
  → extract (fn: writes safety = SAFE|UNSAFE|BLOCK)
  → branch on safety: SAFE → approve   (← replace with your real shell/tool node)
                      UNSAFE/default → block (← replace with a human_gate to escalate)
```

## Run it
```bash
# offline, deterministic (fake twin returns "UNSAFE"); exercises the BLOCK path:
PYTHONPATH=src python3 -m yaah.runtime examples/tool-safety-gate/safety.local.json

# real haiku actually judges the command (needs the `claude` CLI on PATH):
PYTHONPATH=src python3 -m yaah.runtime examples/tool-safety-gate/safety.real.json
```
Swap `fixtures/input.json` to a benign command (e.g. `ls -la`) and the real run
routes SAFE → approve.

## Make it your own (the yaah-native part)
`approve`/`block` are stand-ins. In real use, `approve` becomes the actual
destructive node and `block` becomes a `human_gate`. Because the whole gate is
config (`branch` + a node def + a route map), adding it in front of N existing
stages is an `_extends` overlay — uniform wiring. The *prompt* still needs tuning
per decision-type, so this is "less Python, still per-decision prompt work," not a
free blanket guard. Use it where a cheap pattern-check has real value (destructive
actions, conformance, obvious-error detection) — not as a quality judge.

## Limits — defense in depth, NOT a security boundary
This is a cheap **first-line/advisory filter**, not a security control. A cheap
model can be fooled by an obfuscated command — base64, `$(...)`/command substitution,
a benign-looking wrapper, unicode homoglyphs — and clear it. Treat it as one
inexpensive layer that catches the *obvious* destructive shapes early; keep the real
guarantees (allow-lists, sandboxing, a human gate on truly destructive actions, least-
privilege execution) underneath it. A `SAFE` verdict means "didn't look obviously
dangerous to a small model," not "proven safe."
