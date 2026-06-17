# QUICK START — the runnable starter

Load this when the user accepts the QUICK START offer (Turn 1 of the authoring
skill). It's a *running* artifact — a 5-minute, no-LLM-cost scaffold that proves
the harness works end-to-end — distinct from the authoring judgment in `SKILL.md`.
Write it verbatim, then customize.

### `hello-yaah/`

A `summarize → check → parse → render` pipeline. 6 files, fake provider, one run
command. Demonstrates the **full data-flow contract**: agent → `payload["raw"]`
(a STRING) → validator (retry+feedback) → parse transform (raw → payload keys) →
render, plus the typed-block root shape and console trace.

> **The contract that bites everyone:** an agent's output lands in `payload["raw"]`
> as a string. `json_object` only *validates* it — nothing merges it into the
> payload. Without a parse transform, a downstream `render`/`branch` sees no keys
> and the render FAILS (`render_unfilled_placeholders`) pointing at the missing
> parse — it no longer "succeeds" with un-interpolated `{{placeholders}}` at exit 0.
> Every agent→render or agent→branch edge needs a parse step. See
> [`docs/envelope-by-example.md`](../../../docs/envelope-by-example.md)
> for the real envelope at each hop and
> [`docs/why-yaah.md`](../../../docs/why-yaah.md) for when this engine
> is the right tool.

```
hello-yaah/
├── starter.json                # pipeline (4 stages)
├── starter.local.json          # root (inproc, fake, console trace)
├── hello_transforms.py         # the parse fn (imported from the run dir)
├── prompts/summarize.md        # agent prompt
├── fixtures/input.json         # one envelope
└── templates/output.html       # mustache target
```

**`starter.json`:**
```json
{
  "nodes": {
    "role:summarize": {"type": "agent", "prompt": "file:summarize",
                       "model": "fake:summarize", "stage": "summarize"},
    "role:check":     {"type": "json_object", "required": ["summary"]},
    "role:parse":     {"type": "transform", "target": "fn:hello_transforms:parse",
                       "call": "envelope"},
    "role:render":    {"type": "render", "template_file": "templates/output.html",
                       "out": "summary.html"}
  },
  "graph": {
    "start": "summarize",
    "stages": {
      "summarize": {"node": "role:summarize", "validators": ["role:check"],
                    "max_attempts": 3, "feedback": true, "then": "parse"},
      "parse":     {"node": "role:parse", "then": "render"},
      "render":    {"node": "role:render", "then": null}
    }
  }
}
```

**`hello_transforms.py`** — the `call: "envelope"` signature is
`fn(envelope, config) -> dict`; the returned dict SPREADS over the payload
top-level (that's how `summary` becomes visible to `render` and to any `branch`):
```python
import json

def parse(envelope, config):
    return json.loads(envelope.payload.get("raw", "{}"))
```

**`starter.local.json`** (no `trace` block needed — the harness defaults to a
console sink so first runs are visible by default; add `trace: {sink: [...]}` only
when you want file/langfuse/progress sinks, or `trace: {sink: []}` to opt out).
The scripted table is `by_model`: model name → **LIST of replies, one per attempt**
(a bare string is accepted as a single reply; use a list to script retries):
```json
{
  "transport": {"type": "inproc"},
  "providers": {"fake": {"type": "fake_scripted",
                         "by_model": {"summarize": ["{\"summary\":\"hello\"}"]}}},
  "default_provider": "fake",
  "prompt_sources": {"file": {"type": "file", "dir": "prompts"}},
  "default_prompt_source": "file",
  "state": {"type": "memory"},
  "pipeline": "starter.json",
  "input": "fixtures/input.json",
  "run": true
}
```

- `prompts/summarize.md` → `Summarize {{text}} in one sentence. Return JSON: {"summary": "..."}`
- `fixtures/input.json` → `{"text": "YAAH is a domain-free harness."}`
- `templates/output.html` → `<h1>{{summary}}</h1>`

Expected: `[trace]` lines for all four stages, exit 0, and `summary.html`
containing `<h1>hello</h1>` (if the render fails with `render_unfilled_placeholders`,
your parse stage is missing or misordered).

**Run** — install yaah once, then invoke `yaah` directly:
```bash
# one-time install (editable, from the yaah repo root)
( cd /abs/path/to/yaah && pip install -e . )

# every run
cd hello-yaah && yaah starter.local.json
```

If you can't install (read-only checkout, vendor restrictions), the equivalent
uninstalled invocation still works:
```bash
REPO=/abs/path/to/yaah
PYTHONPATH="$REPO/src" python3 -m yaah.runtime starter.local.json
```

To go real: add a `claude` provider (`{"type": "claude_cli"}`), swap
`fake:summarize` → `claude:claude-sonnet-4-6`. Same pipeline.
