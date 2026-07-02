You are authoring a YAAH pipeline configuration from a plain-language request.
YAAH pipelines are pure JSON: a PIPELINE config (workers + wiring) and a ROOT
config (how to run it). Your draft is checked by the engine's own validator;
if it fails you will be re-asked with the exact errors.

# Request

{{request}}

# Output — STRICT JSON, nothing else

Reply with exactly ONE JSON object. No prose, no markdown fences, no comments:

{
  "name": "<short kebab-case name for the pipeline, e.g. summarize-review>",
  "root": { <the root config> },
  "pipeline": { <the pipeline config> }
}

# The minimal config grammar

A PIPELINE config has exactly two keys:

- "nodes" — the workers, keyed by role id: {"role:<id>": {"type": ..., ...}}
- "graph" — the wiring: {"start": "<stage>", "stages": { "<stage>": {...} }}

Node types you may use:

- agent — calls a model; its JSON reply keys are merged onto the payload:
  {"type": "agent", "prompt": "file:<key>", "model": "claude:claude-sonnet-4-6", "stage": "<label>"}
  (prompts are ALWAYS file references like "file:summarize" — never inline prose)
- human_gate — parks the run for a human decision:
  {"type": "human_gate", "awaiting": "<tag>", "form": "approve_or_revise", "ask": "<question>"}
- render — fills a template with payload keys and writes a file:
  {"type": "render", "template_file": "templates/<file>", "out": "<output file>"}

Each STAGE wires one node and says where the run goes next:

- linear step:            {"node": "role:<id>", "then": "<next stage>"}   ("then": null ends the run)
- retry with feedback:    add "max_attempts": 2, "feedback": true to an agent stage
- route on a value:       {"node": "role:<id>", "branch": {"on": "<payload key>", "routes": {"<value>": "<stage>"}, "default": "<stage>"}}

HARD RULES the validator enforces — a violation fails your draft:

1. "start", every "then", and every branch route/default must name a DECLARED stage.
2. Every stage's "node" must name a DECLARED node.
3. A human_gate stage must "branch" on "decision" (routes for the human's
   choices, e.g. {"revise": "<redraft stage>"} with "default" as the approve path).
   A gate with only "then" ignores the human's reject.

A ROOT config says how to run the pipeline. Adapt this template (keep the
shape; change the "pipeline" filename to <name>-pipeline.json):

{
  "transport": {"type": "inproc"},
  "providers": {"claude": {"type": "claude_cli"}},
  "default_provider": "claude",
  "prompt_sources": {"file": {"type": "file", "dir": "prompts"}},
  "default_prompt_source": "file",
  "state": {"type": "file", "dir": "state"},
  "pipeline": "<name>-pipeline.json",
  "run": true
}

# Worked example — the smallest valid draft (one agent, linear)

{"name": "hello", "root": {"transport": {"type": "inproc"}, "providers": {"claude": {"type": "claude_cli"}}, "default_provider": "claude", "prompt_sources": {"file": {"type": "file", "dir": "prompts"}}, "default_prompt_source": "file", "state": {"type": "file", "dir": "state"}, "pipeline": "hello-pipeline.json", "run": true}, "pipeline": {"nodes": {"role:greet": {"type": "agent", "prompt": "file:greet", "model": "claude:claude-sonnet-4-6", "stage": "greet"}}, "graph": {"start": "greet", "stages": {"greet": {"node": "role:greet", "max_attempts": 2, "feedback": true, "then": null}}}}}

If FEEDBACK is appended below, a previous attempt FAILED the validator: fix
every named problem and re-emit the FULL corrected JSON object, not a diff.
