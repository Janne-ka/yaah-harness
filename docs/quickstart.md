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
[trace] stage parse ok (1ms)
[trace] stage render ok (1ms)
RESULT: Done(output=Envelope(... payload={'summary': 'hello', ...}))

$ cat summary.html
<h1>hello</h1>
```

## 3. What just happened

One message (an **Envelope**) flowed through four steps: **summarize** (an agent)
→ **check** (a validator) → **parse** → **render**. Watch its `payload` change:

```
input               {"text": "YAAH is a domain-free harness."}
after summarize     {"raw": "{\"summary\": \"hello\"}"}     ← the agent's answer, a STRING
after parse         {"summary": "hello"}                    ← now it's a real key
after render        {"summary": "hello", "output": "<h1>hello</h1>", ...}
```

The thing to remember: an agent hands you a *string* in `raw`. A **parse** step
turns it into usable keys — without it, `render` fails (`render_unfilled_placeholders`), telling you the parse step is missing.

That's six small files in `examples/hello-yaah/`: a pipeline, a root config, the
parse function, a prompt, an input, a template. The [tutorial](tutorial.md) builds
on it step by step.

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
