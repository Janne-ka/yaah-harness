# How it fits together (a from-zero walkthrough)

You don't need to know anything about YAAH to read this. By the end you'll know
how a JSON file, a Python file, and an AI model combine into one running pipeline
— using the **arch-drift** example as the worked case.

---

## The one idea

A YAAH pipeline is an **assembly line** for a piece of work. Each station does
one small job and passes a tray of data to the next station. Some stations run
plain Python; some ask an AI model; one can stop and wait for a human.

You write **two files**:
- a **JSON file** that describes the line — the stations and their order;
- a **Python file** next to it with the code for the stations that run code.

YAAH reads the JSON and runs the line. That's the whole trick: *the JSON is the
structure, the Python is the behavior, and a shared "tray" of data carries the
work between stations.*

Three words cover the entire vocabulary:

| word | plain meaning |
|------|---------------|
| **payload** | the tray of data (just key → value) that travels station to station |
| **node** | a station. A handful of built-in kinds: `transform` (run your Python), `agent` (ask a model), `render` (fill a template into a file), `human_gate` (pause for a person) |
| **graph** | the running order — what's next, and where the line forks |

---

## The example: arch-drift

arch-drift keeps a project's **architecture diagram honest**. It reads the code,
asks a model to draw the architecture as a diagram, compares that to the diagram
currently committed in the repo, and — if they differ — asks a human to approve
the update before saving it.

```
snapshot → read current diagram → ASK MODEL → render → compare ┬─ same? → done
  (code)                          (draw it)          (diff)    │
                                                               └─ changed? → report → ASK HUMAN ─ approve → save
                                                                                              └ revise → back to the model
```

Two files do all of it, in the same folder:
- `arch-drift-pipeline.json` — the assembly line.
- `transforms.py` — the code stations (one Python function per code step).

---

## Tie #1 — a JSON station points at a Python function

Here is one station in the JSON:

```json
"role:snapshot": {"type": "transform", "target": "fn:transforms:snapshot", "call": "envelope"}
```

Read it as: *this station runs code (`transform`); the code is the `snapshot`
function in `transforms.py` (`fn:transforms:snapshot`).*

- **`fn:transforms:snapshot`** means "the `snapshot` function in the module
  `transforms`." YAAH looks for `transforms.py` **right next to this JSON file** —
  that's why the two files live together.
- **`"call": "envelope"`** tells YAAH how to call it: hand the function the whole
  tray. So the function looks like `def snapshot(envelope, config): ...`.

That same `fn:...` reference shows up in a few spots — a transform's `target`, a
model stage's `attach:` (a cost meter, more below), and the fan-in's `reduce:`
(the step that merges two parallel branches). Same idea every time: *JSON names a
Python function.*

---

## Tie #2 — stations talk through the tray (the payload)

A code station **returns a dictionary**, and YAAH lays those keys onto the tray.
The next station reads keys back off the tray. That's the entire conversation
between stations.

Three ways a station reads the tray:
- a **model prompt** or an **HTML template** uses `{{key}}` — YAAH fills it in
  from the tray;
- a **branch** reads one key by name to decide where to go next;
- a **code** station just reads `envelope.payload["key"]`.

Follow one real handoff:

```
snapshot()      puts  {snapshot: "...repo summary...", feedback: ""}  on the tray
   ↓
the model stage's prompt contains  {{snapshot}}  →  YAAH fills it with that text
   ↓
the model replies with JSON like {"mermaid": "..."}  →  YAAH drops `mermaid` on the tray
   ↓
render_mermaid()  reads  payload["mermaid"],  returns  {new_svg: "<svg…>"}
   ↓
diff_svgs()  compares old vs new,  returns  {changed: "yes", summary: "…"}
```

The tightest link is the **branch**. `diff_svgs` puts `changed: "yes"` (or
`"no"`) on the tray, and the graph routes on that exact key:

```json
"diff": {"branch": {"on": "changed", "routes": {"yes": "report", "no": "done"}}}
```

*The function's output key (`changed`) and the branch's `on` key are the same
string.* That's the handshake.

---

## Tie #3 — the model stage

One station asks the AI model. In the JSON it's an `agent`:

```json
"role:extract": {"type": "agent", "prompt": "file:extract",
                 "model": "claude:claude-sonnet-4-6", "max_attempts": 2, "feedback": true}
```

- `"prompt": "file:extract"` — the prompt text lives in `prompts/extract.md` (a
  file, not inline), and it contains `{{snapshot}}`, which YAAH fills from the
  tray.
- The model answers with JSON. YAAH **parses it and lays the keys on the tray for
  you** (so `render_mermaid` finds `mermaid` directly — no glue code).
- `"max_attempts": 2, "feedback": true` — if the model's answer doesn't parse,
  YAAH retries *and tells the model what was wrong.* You didn't write that loop;
  the harness owns it.

The model is just **one station**. Everything around it — feeding it the right
text, retrying, moving on — is declared in the JSON, not coded.

---

## The graph is the running order

The `nodes` section says *what each station is*. The `graph` section says *what
runs next*:

```json
"graph": {
  "start": "snapshot",
  "stages": {
    "snapshot": {"node": "role:snapshot", "then": "read-svg"},
    "extract":  {"node": "role:extract",  "then": "render-svg"},
    "diff":     {"node": "role:diff", "branch": {"on": "changed", "routes": {"yes": "report", "no": "done"}}},
    "gate":     {"node": "role:gate", "branch": {"on": "decision", "routes": {"approve": "land", "revise": "extract"}}}
  }
}
```

`then` is "go straight here next." `branch` is "read this tray key and pick a
route." Notice the gate's `revise` route goes *back to* `extract` — that's the
human-feedback loop, declared as one line.

---

## Why a JSON file at all — why not just write Python?

Short version: **Python writes the work (that's `transforms.py`); the JSON
declares the wiring.** Declaring the wiring as *data* is what lets YAAH do things
to your pipeline it could never do to code — most visibly here, the **human
gate**: when this run pauses for review, the whole half-finished run is saved to
disk and can resume in a different process days later. Retries, cost tracking,
model-swap, and *safe AI-authored edits* all come from the same place.

The full argument — why "just data" matters for **safety, testing, and tooling** —
is its own short read: **[Why a YAAH pipeline is data, not
code](../../docs/why-data-not-code.md)**.

---

## Run it yourself (offline, no API key)

```bash
MERMAID_RENDERER=:canned yaah run examples/arch-drift/arch-drift.local.json
```

It runs the line on a fake model, stops at the human gate, and tells you the
baton id. Approve it to finish:

```bash
echo '{"decision":"approve"}' > /tmp/d.json
yaah resume examples/arch-drift/arch-drift.local.json <baton-id> /tmp/d.json
```

Now open `transforms.py` and `arch-drift-pipeline.json` side by side and trace
one key — say `changed` — from the function that writes it to the branch that
reads it. Once you see that handshake, you've got the whole model.
```
