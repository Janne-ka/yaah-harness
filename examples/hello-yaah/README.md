# hello-yaah — the smallest pipeline

Take some text, ask a model to summarize it, write the summary to an HTML file.
Two stages, no Python. **If you read one example first, read this one.**

## Run it (offline, no API key)

```
pip install -e .                                    # one-time: puts the `yaah` command on PATH
yaah run examples/hello-yaah/starter.local.json
```

Not installing? The same run without the `yaah` command:
`PYTHONPATH=src python3 -m yaah.runtime examples/hello-yaah/starter.local.json`

Writes `summary.html`. `starter.local.json` uses a *fake* model that returns a
canned summary, so it runs with no key and no network.

## What's in the folder

- `starter.json` — the **pipeline**: the two stages and their order.
- `starter.local.json` — the **root config** that runs the pipeline offline (the
  fake model, the input).
- `prompts/summarize.md` — the model's instructions.
- `templates/output.html` — the HTML to fill in.
- `fixtures/input.json` — the input: `{"text": "..."}`.

## How it ties together

There's a tray of data — the **payload** — passed from stage to stage. Each stage
reads keys off it and puts keys back on it:

```
input  {text: "..."}
  │
  ▼  summarize  (an `agent` stage)
        its prompt is "Summarize {{text}} ...":  YAAH fills {{text}} from the tray.
        the model replies  {"summary": "..."}  and YAAH lays `summary` on the tray
        for you — that's "parse-by-default", no glue code.
  │
  ▼  render
        its template is  <h1>{{summary}}</h1>:  YAAH fills {{summary}} from the tray
        and writes summary.html.
```

The whole trick: **a key one stage produces (`summary`) is the key the next stage
reads (`{{summary}}`).** That handshake — the JSON declares the stages, the payload
carries the data between them — is all of YAAH. Everything bigger is more of the
same.

Two touches worth noticing in `starter.json`:

- `"max_attempts": 3, "feedback": true` on the summarize stage — if the model's
  reply isn't valid JSON, YAAH retries *and tells the model what was wrong.* You
  didn't write that loop.
- `"model": "fake:summarize"` — swap it for a real `claude:...` model (in a real
  root config) and the *same* pipeline runs for real. The model is one swappable
  part, not the structure.

## Bonus: watch it live (monitoring)

`starter-live.local.json` is the same run with the optional `live` trace capture
turned on — the console then shows what the model is doing *mid-call*, not just
which stage finished:

```
$ python3 -m yaah.runtime starter-live.local.json
[trace] live summarize: turn started
[trace] live summarize: generating (19 chars)
[trace] live summarize: turn done (end_turn)
[trace] stage summarize ok (1ms)
[trace] stage render ok (1ms)
```

The whole feature is one config line — `"trace": {"capture": ["phase", "live"], ...}`
— and fully optional: drop the flag and nothing fires. For a cloud/remote run,
swap the console sink for `{"type": "file", "path": "trace.jsonl"}` and
`tail -f` it (or a NATS transport to watch from another machine): a stage that
shows `turn started` with no `turn done` and a stalled heartbeat is hung; a
growing `generating (N chars)` is alive. Sizes and names only — model text never
enters the trace.

**Heartbeat honesty (check your backend):** the pulse can only prove liveness
while the backend streams incrementally. The offline demo's scripted provider
returns its text in one piece, so you see a single `generating` line. `litellm`
defaults to the same single-shot shape — add `"stream": true` to its provider
block for real SSE chunking (an actual growing heartbeat). `claude_cli` streams
text for real and reports its internal tool activity as `tool X` / `tool X
returned` pulses that bracket each tool run — the gap between the two is the
tool executing, not a hang. The full per-backend list lives in
`src/yaah/agents/live_events.py` (KNOWN LIMITS).

## Next

- **Why is the pipeline a JSON file and not just Python?** →
  [`docs/why-data-not-code.md`](../../docs/why-data-not-code.md)
- **A bigger pipeline traced end to end** (human gate, A/B, cost) →
  [`examples/arch-drift/HOW-IT-FITS-TOGETHER.md`](../arch-drift/HOW-IT-FITS-TOGETHER.md)
- **Build your own** → [`docs/tutorial.md`](../../docs/tutorial.md) +
  [`docs/archetypes.md`](../../docs/archetypes.md)

## Bonus: your first A/B campaign

This directory doubles as the `yaah ab` example — same pipeline, two
summarizer models, compared as data:

```
yaah ab experiment.json             # 2 variants x 3 reps -> durable rows in .ab/
yaah ab experiment.json --report    # the comparison matrix (no winner column — you decide)
yaah ab experiment.json --rescore contract-scored.json   # would a tightened
                                    # output contract reject old outputs? zero model calls
```

The B variant is a two-file `_extends` overlay (`starter-b.local.json` +
`starter-b.json`) — the same mechanism you'd use to promote it: point
production at B's files when the matrix says so. Expect two
`[ab: metric-unproven]` warnings on stderr: the `score` metric is real but
undeclared in the agents' `output_schema` — declaring it there silences them
(and is the right move in production). Full story:
[`docs/ab-experiments.md`](../../docs/ab-experiments.md).
