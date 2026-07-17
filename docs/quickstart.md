# Quickstart

Run a real YAAH pipeline in about five minutes — no API key, no LLM cost. It uses
a **fake** model backend that replays scripted responses, so the whole data-flow
is real but free and deterministic. When you're ready, swapping in a real model is
a one-line change.

## 1. Install

YAAH targets **Python 3.9+** and the core has **zero runtime dependencies**.

```bash
git clone <this-repo> yaah && cd yaah
pip install -e .          # add [all] for every adapter: pip install -e ".[all]"
```

(Real model backends, NATS, Langfuse, and HTTP are opt-in
[extras](../README.md#environments--dependency-hardening-pixi) — you don't need
them for this.)

## 2. Run the hello-yaah example

```bash
cd examples/hello-yaah
yaah run starter.local.json
```

(Step 1 installed the `yaah` console script. Not pip-installed? `python3 -m
yaah.runtime starter.local.json` is the equivalent; from a source checkout
prefix `PYTHONPATH=src`.)

You should see each stage trace, then `RESULT: Done`, and a `summary.html` file:

```
[trace] stage summarize ok (1ms)
[trace] stage render ok (1ms)
RESULT: Done(output=Envelope(... payload={'summary': 'hello', ...}))

$ cat summary.html
<h1>hello</h1>
```

## 3. What just happened

One message (an **Envelope**) flowed through two steps: **summarize** (an agent)
→ **render**. Watch its `payload` change:

```
input               {"text": "YAAH is a domain-free harness."}
after summarize     {"raw": "{\"summary\": \"hello\"}", "summary": "hello"}
                                                       ↑ parsed by the agent (ADR-0004)
after render        {"raw": "...", "summary": "hello", "output": "<h1>hello</h1>", ...}
```

The agent parses its JSON reply by default (`parse: true`) — the parsed keys land
directly on the payload alongside `raw`, so `render` finds `summary` without a
separate parse stage. The one thing to watch: the agent's reply **replaces** the
whole incoming payload, so any upstream key a later stage reads needs a `"carry": [...]`
declaration on the agent, or it disappears.

That's five small files in `examples/hello-yaah/`: a pipeline, a root config, a
prompt, an input, a template. The [tutorial](tutorial.md) builds on it step by step.

## 4. Make it real

In `starter.local.json`, replace the fake provider with a real one, and in
`starter.json` point the agent at a real model:

```jsonc
// starter.local.json
"providers": {"claude": {"type": "claude_cli"}},
"default_provider": "claude",

// starter.json — role:summarize
"model": "claude:claude-sonnet-4-6"
```

Run it the same way (you'll need the `claude` CLI authenticated, or use
`litellm` via `pip install -e ".[litellm]"` and a `litellm` provider). The
pipeline is unchanged — only the backend swapped.

## Next

- **[Tutorial](tutorial.md)** — build up from this pipeline: validators & retry,
  conditional branching, a human approval gate with durable suspend/resume, going
  real, and tracing.
- **[Node reference](node-reference.md)** · **[Root-config reference](root-config-reference.md)** — every node type and config key.
- **[Why YAAH](why-yaah.md)** · **[Design](design.md)** — the rationale and the architecture.
