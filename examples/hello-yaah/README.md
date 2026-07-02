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

## Next

- **Why is the pipeline a JSON file and not just Python?** →
  [`docs/why-data-not-code.md`](../../docs/why-data-not-code.md)
- **A bigger pipeline traced end to end** (human gate, A/B, cost) →
  [`examples/arch-drift/HOW-IT-FITS-TOGETHER.md`](../arch-drift/HOW-IT-FITS-TOGETHER.md)
- **Build your own** → [`docs/tutorial.md`](../../docs/tutorial.md) +
  [`docs/archetypes.md`](../../docs/archetypes.md)
