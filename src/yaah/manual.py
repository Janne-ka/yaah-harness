"""build_manual() — the ONE generated, token-budgeted agent manual for LLMs
authoring yaah configs (the llms.txt idea).

Used by: the `yaah manual` CLI action — NOT wired here: cli.py calls
`build_manual() -> str` (this module's whole public API) and prints it.
Tests in tests/test_manual.py.
Where: a read-only projector over the SAME tables the validator enforces —
`build.builders.default_registry()` for node types, `validate`'s key tables
and `runtime_factories`' factory maps for the root-config surface. Fixed
prose covers only what has no table (mental model, rules that bite, the
example — which the test round-trips through validate_root/validate_pipeline
so it can never rot).
Why: a model with this single document in context can author a valid
root + pipeline config without reading the repo; every listable fact is
projected at call time, so the manual can never drift from the code (the
sink/sinks bug class). Budget: under ~24k chars (~6k tokens), test-asserted.

Targets Python 3.9+.
"""
from __future__ import annotations

import inspect
import json
import re
import sys
from typing import Any, Dict, List, Optional

from .build.builders import default_registry

_HEADER = """\
# yaah — agent manual (generated; do not edit)

yaah is a generic, domain-free runtime for orchestrating agentic workers: the
harness owns routing and control, a worker (including an LLM agent) does one
job and is interchangeable. Wiring is data (JSON), work is code. The core has
zero runtime dependencies; every third-party integration is an opt-in adapter.

Mental model: an **Envelope** (one message shape) flows stage -> stage; a
**Node** is `invoke(input, config) -> output`; a **pipeline** is JSON —
`nodes` (role -> type + config) wired by a `graph` (`then` / `branch` /
`fork`+`fanin`). A **root config** says how to run it (transport, providers,
which pipeline + input). Workers never address each other; the harness routes.
"""

_RULES = """\
## Rules that bite

- **Agent output is parse-by-default** (ADR-0004): the model text lands in
  `payload["raw"]` AND its parsed JSON keys auto-merge onto the payload
  (fence-tolerant), so `agent -> render`/`branch` just works. With
  `"parse": false` the load-time linter REQUIRES a `transform` between the
  agent and any render/branch.
- **A human gate must `branch` on `decision`** — a gate with only `then` is a
  pause, not a gate; the human's reject is ignored.
- **`fn:module:func` targets resolve relative to the config's directory** —
  keep `transforms.py` next to the config, or package shared code and use a
  dotted path. Config is trusted code: never let payload values reach it.
- **Always ship a `.fake.json` overlay** (`_extends` the canonical config,
  swap models to `fake:*`) so the pipeline runs offline/CI for free.
- **Generate -> validate -> repair**: check every draft with
  `yaah validate <root> --json` before handing it over.
"""

_TRANSFORM_CONVENTIONS = """\
## Transform calling conventions

A `transform` node has two call modes, set by `call:` (default `"args"`):

**`call: "args"` (default)** — enriching. fn signature: `func(args)` — one
argument. `args` = the whole payload dict, or the value at `args_from` key if
declared. Return value: anything; nested under `into` (default `"result"`).
Existing payload keys are kept — the result is ADDED, not replacing.

  `{"type": "transform", "target": "fn:app.transforms:classify", "into": "category"}`

  After invoke: `{"raw": "...", "category": <return value>, ...prior keys}`.

**`call: "envelope"` (fn: only)** — replacing. fn signature:
`func(envelope, config)` — two arguments. Return a dict or an Envelope.
A returned dict BECOMES the new payload entirely (does not merge into the old
one). Copy prior keys explicitly if needed:
`return {**envelope.payload, "new_key": value}`.
A returned Envelope passes through unchanged.

  `{"type": "transform", "call": "envelope", "target": "fn:app.transforms:merge_findings"}`

  `transforms.py`: `def merge_findings(envelope, config): return {**envelope.payload, "report": build(envelope.payload["results"])}`

Use `"args"` for pure computations (the common case). Use `"envelope"` when the
fn needs `config.extras` or must control the full output shape.
"""

_FOREACH = """\
## foreach — dynamic per-item fan-out

`foreach` is the third parallel shape (beside `fanout` and `fork`/`fanin`): it
maps **one node over every element of a runtime-sized list**, bounded to K
concurrent. Use it when the item count is not known at author time.

```
"swarm": {
  "node": "skeptic",
  "foreach": {"items": "requirements", "into": "item",
              "carry": ["doc", "task"], "max_concurrent": 3},
  "min_success": 5,
  "then": "consolidate"
}
```

Key facts for authors:

- **One node, one hop.** Each element is processed by the same node role once.
  There is no per-item sub-graph; multi-stage-per-item work requires a cursor
  back-edge loop over the list instead.
- **Per-item input is replace + named carries only.** The item payload is
  `{ <into>: items[i], "item_index": i, <carry keys>, <graph.sticky keys> }` —
  NOT a copy of the full inbound payload (a 50 KB doc × 200 items would be an
  invisible 200× cost). Name every key you need in `foreach.carry`.
- **Output.** The merged stage output = inbound payload PLUS `results`
  (list of `{"item_index": int, "payload": dict}` pairs, in item order) and
  `failed_items` (list of indexes that failed). Pairs, not bare payloads —
  a reset agent drops `item_index` from its output, so bare compaction would
  misalign.
- **An AWAIT item parks the whole stage.** Per-item gates are a v1 non-goal;
  if one item suspends, the entire `foreach` stage parks.
- **`feedback` keys are not threaded into per-item inputs.** The retry
  feedback is the whole prior merged swarm, semantically odd per item — v1
  documented limitation. Use `min_success` for partial-failure tolerance.
- **For multi-stage-per-item work**, use a cursor back-edge loop. See the
  recipe below — the cursor step MUST be `call:"envelope"`, not the default
  `call:"args"`. Read the pitfall note carefully before authoring.

### Cursor back-edge loop recipe

Shape: `advance` (envelope-transform) → `process` (agent or transform) →
`collect` (envelope-transform) → back to `advance`; exits when
`advance` sets `loop_done:"yes"`.

```
advance → branch on loop_done
  "yes" → report  (terminal)
  default → process → collect → advance  (back-edge)
```

**Stage wiring** (inline keys, no extra JSON fences):

`"advance": {"node": "role:advance", "branch": {"on": "loop_done", "routes": {"yes": "report"}, "default": "process"}}`

`"process": {"node": "role:process", "then": "collect"}`

`"collect": {"node": "role:collect", "then": "advance"}`

**Node config for advance** — `call:"envelope"`, `provides` declared:

`{"type": "transform", "call": "envelope", "target": "fn:transforms:advance", "provides": ["cursor", "current_text", "loop_done"]}`

**Advance function** (`transforms.py`, copy verbatim):

`def advance(envelope, config):`
`    p = envelope.payload`
`    items, cursor = list(p.get("items") or []), int(p.get("cursor", 0))`
`    if cursor < len(items):`
`        return {**p, "cursor": cursor + 1, "current_text": items[cursor], "loop_done": "no"}`
`    return {**p, "current_text": "", "loop_done": "yes"}`

**`graph.sticky`** must list every key that must survive a payload-replacing
stage (agents replace the payload): `["items", "cursor", "collected", "current_text"]`.

**Seed these in `input`:** `{"items": [...], "cursor": 0, "collected": []}`.

**Critical pitfall — call:"args" spins forever.** The default `call:"args"`
transform nests its return dict UNDER an `into` key (default `"result"`). A
branch that reads `{{loop_done}}` gets a missing key, falls through to
`default` every pass, and the loop never terminates (verified: 3334+
iterations until the 10,000-step livelock backstop fires). A `call:"envelope"`
transform's return dict BECOMES the new payload (spread flat), so
`{{loop_done}}` resolves immediately. Rule: every cursor step in a back-edge
loop MUST use `call:"envelope"` and copy prior keys with `{**envelope.payload, ...}`.
"""

_PLACEHOLDERS = """\
## Placeholders — flat keys only

Template substitution in agent prompts, `render` templates, and `human_gate`
`ask` strings uses `{{key}}` syntax.

**Key names are single flat payload keys.** The regex is `\\w+` — dot-paths
are not supported. `{{item.text}}` NEVER matches; it passes through as the
literal string `{{item.text}}` in the rendered output, silently. If you need
a nested value, flatten it to a top-level key with an upstream `transform`.

**Agent-prompt dialect (agent node only):**
- `{{?key}}` — optional; renders EMPTY when absent instead of faulting under
  `strict_render`. Use for keys that are only present on later passes (e.g.
  `{{?loop_feedback}}`).
- `{{!key}}` — untrusted; the value is fenced with an unguessable token to
  block instruction injection. Use for any agent-authored or user-supplied text.
- `{{?!key}}` or `{{!?key}}` — optional AND untrusted (order doesn't matter).

**`render` and `human_gate` templates use the plain templater**: `?`/`!` sigils
are NOT recognised there — `{{!key}}` is a literal. Only `{{key}}` (`\\w+`)
substitutes. This is deliberate: a render's output feeds a human or a file,
not a model prompt, so fencing has no consumer.
"""

_FAKE_SCRIPTED = """\
## Offline scripted replies — fake_scripted contract

`fake_scripted` delivers canned strings per model name. Calls consume replies
in order. **Lists are exhausted, not cycled.** When the list runs out:

- default — returns empty string (causes `not_json` failure downstream for
  parse-mode agents; useful to catch under-sized lists in CI).
- `on_exhaustion: "repeat_last"` — returns the final entry for every
  subsequent call. Use for loops, backward edges, or any run that may
  re-enter the scripted stage.
- `on_exhaustion: "raise"` — raises IndexError loud. Reserve for
  deterministic single-pass runs only; a mid-run gate park + cross-process
  resume will replay from entry 0 and raise instead of completing.

**Size for the worst case.** A retry loop over a 7-item foreach that may
retry once needs 14 entries, not 7.

**The cursor is process-local.** It resets to 0 when a new process starts.
A run that parks at a human gate and resumes in a new process replays from
the beginning. Practical rule: order gated work last so no scripted agent
stage runs after a gate, or drive the gate in the same process.
"""

_FOOTER = """\
## Repair loop

Run `yaah validate <root-config> --json`, patch the config from each
`errors[].message` (and its `stage`), re-run until the report is clean.
Errors carry did-you-mean hints; fix ALL of them in one pass — the validator
gathers every problem per run.
"""

_ON_ERROR = ('`"clear"` (default — clear the stage\'s worktree/state) | `null` '
             '(opt out) | `{"compensate": "fn:mod:func"|"node:role"|"http:...", '
             '"on_compensate_fail": "error"|"warn"}`')

_ROOT_GLOSS = {
    "pipeline": "path to the pipeline JSON, or an inline pipeline object",
    "input": "fixture path or inline object — the run's first payload",
    "decisions": "decision-fixture path for scripted human gates",
    "serve": "serve node role(s) as a remote worker instead of driving a run",
    "baton_ttl": ("seconds a PARKED HUMAN GATE stays claimable before it is "
                  "abandoned; default 259200 (72h)"),
    "checkpoint_ttl": ("seconds a RUNNING CHECKPOINT stays recoverable — the clock "
                       "restarts each completed stage, so it bounds how long ONE "
                       "stage may be in flight; absent = inherit baton_ttl "
                       "(recommended explicit value: 21600 = 6h)"),
    "lease_horizon": ("seconds a lease from ANOTHER HOST may go unrefreshed before "
                      "its process is presumed dead and its run recoverable; "
                      "default 3600. Must fit checkpoint_ttl"),
    "lease_host": ("what this deployment calls THIS host in a lease owner id; "
                   "default socket.gethostname(). Set it in a CONTAINER, where "
                   "gethostname() is the pod id and changes every restart — which "
                   "makes every own run look FOREIGN and disables the pid probe. "
                   "Use a stable identity that is unique per kernel (the node name)"),
    "run_dir": ("per-run artifact root the `{run_dir}` node-spec macro expands to "
                "(base-relative or absolute); no default — using the macro without "
                "it is a build error"),
    "plugins": "module paths imported before validation (register_type extensions)",
}

_EXAMPLE_ROOT: Dict[str, Any] = {
    "transport": {"type": "inproc"},
    "state": {"type": "memory"},
    "providers": {"fake": {"type": "fake_scripted",
                           "by_model": {"summarize": ["{\"summary\": \"stub\"}"]}}},
    "default_provider": "fake",
    "prompt_sources": {"static": {"type": "static", "prompts": {
        "summarize": "Reply as JSON {\"summary\": \"...\"} for:\n{{text}}"}}},
    "default_prompt_source": "static",
    "pipeline": "pipeline.json",
    "input": {"text": "hello world"},
    "run": True,
}

_EXAMPLE_PIPELINE: Dict[str, Any] = {
    "nodes": {
        "role:summarize": {"type": "agent", "prompt": "static:summarize",
                           "model": "fake:summarize", "stage": "summarize"},
        "role:check": {"type": "json_object", "required": ["summary"]},
        "role:report": {"type": "render", "template_text": "Summary: {{summary}}",
                        "out": "summary.txt"},
    },
    "graph": {
        "start": "summarize",
        "stages": {
            "summarize": {"node": "role:summarize", "validators": ["role:check"],
                          "max_attempts": 3, "feedback": True, "then": "report"},
            "report": {"node": "role:report", "then": None},
        },
    },
}


def _sentence(doc: Optional[str]) -> str:
    text = " ".join((doc or "").split())
    head = text.split(". ", 1)[0].rstrip(".")
    if " — " in head:  # docstrings open "ClassName — what it is"; keep the what
        head = head.split(" — ", 1)[1]
    return head[:140]


def _node_doc(builder: Any) -> str:
    """One-liner for a node type: the constructed class's docstring (falling back
    to its module docstring), found live by resolving the first `ClassName(` in
    the builder's source against the builder's own module globals — survives a
    registry swap, degrades to the builder's docstring when unreadable."""
    try:
        src = inspect.getsource(builder)
        mod_globals = sys.modules[builder.__module__].__dict__
        for name in re.findall(r"\b([A-Z][A-Za-z0-9_]*)\(", src):
            obj = mod_globals.get(name)
            if isinstance(obj, type) and getattr(obj, "__module__", "").startswith("yaah"):
                return _sentence(obj.__doc__ or sys.modules[obj.__module__].__doc__)
    except (OSError, TypeError, KeyError):
        pass
    return _sentence(getattr(builder, "__doc__", None))


def _node_section() -> List[str]:
    reg = default_registry()
    out = ["## Node types (pipeline `nodes.<role>.type`)", ""]
    for name in sorted(reg._builders):
        doc = _node_doc(reg._builders[name])
        out.append("- `{}`{}".format(name, " — " + doc if doc else ""))
    return out + [""]


def _typed_lines(type_map: Dict[str, Any]) -> List[str]:
    out = []
    for t in sorted(type_map):
        keys = type_map[t][1]
        spec = ("open spec — constructor enforces keys" if keys is None
                else ("keys: " + ", ".join(sorted(keys)) if keys else "no extra keys"))
        out.append("  - `{}` — {}".format(t, spec))
    return out


def _root_section() -> List[str]:
    from . import runtime_factories as rf
    from . import validate as v
    from .trace.contributors import BUILTIN_CONTRIBUTORS
    out = ["## Root config (how to run)", "",
           "All top-level keys (anything else is rejected; `$schema` and "
           "`_`-prefixed comment keys are ignored): "
           + ", ".join("`{}`".format(k) for k in sorted(v._ROOT_KEYS)), ""]
    out.append("Typed blocks (`{\"type\": ...}`):")
    for block, type_map in (("transport", rf._TRANSPORT_TYPES),
                            ("state", rf._STATE_TYPES)):
        out.append("- `{}`:".format(block))
        out.extend(_typed_lines(type_map))
    out.append("")
    out.append("Named maps (`{\"<name>\": {\"type\": ...}}`); each `default_*` "
               "string key must name a declared entry of its map "
               "(" + ", ".join("`{}`".format(k) for k in sorted(v._STRING_KEYS)) + "):")
    for block, map_name in sorted(v._NAMED_MAP_FACTORIES.items()):
        out.append("- `{}`:".format(block))
        out.extend(_typed_lines(getattr(rf, map_name)))
    out.append("")
    out.append("`trace` block — keys: " + ", ".join(sorted(rf._TRACE_KEYS))
               + "; modes: " + ", ".join(rf._TRACE_MODES)
               + "; capture names: " + ", ".join(sorted(BUILTIN_CONTRIBUTORS))
               + "; sink types:")
    out.extend(_typed_lines(rf._TRACE_SINK_TYPES))
    out.append("")
    out.append("Bool keys: " + ", ".join("`{}`".format(k) for k in sorted(v._BOOL_KEYS)) + ".")
    for k in sorted(_ROOT_GLOSS):
        if k in v._ROOT_KEYS:
            out.append("- `{}` — {}".format(k, _ROOT_GLOSS[k]))
    out.append("")
    out.append("Defaults applied when omitted: `{}`".format(
        json.dumps(v._DEFAULTS, sort_keys=True)))
    return out + [""]


def _pipeline_section() -> List[str]:
    from . import validate as v
    return [
        "## Pipeline config (nodes + graph)", "",
        "`nodes` maps a role name to `{\"type\": <node type>, ...config}`; "
        "`graph` wires stages. Graph keys: "
        + ", ".join("`{}`".format(k) for k in sorted(v._GRAPH_KEYS)) + ".", "",
        "Stage keys: " + ", ".join("`{}`".format(k) for k in sorted(v._STAGE_KEYS)) + ".", "",
        "Routing: `then` names the next stage (null = end); `branch` is "
        "`{\"on\": <payload key>, \"routes\": {value: stage}, \"default\": stage}`; "
        "`fork` lists stage chains rejoined by a `fanin` stage "
        "(`{\"expect\": [stages]}`); `fanout` is a one-stage barrier over node "
        "roles. `validators` lists validator node roles retried up to "
        "`max_attempts` with `feedback` to the agent. Every target must "
        "resolve to a declared stage/node.", "",
        "`on_error`: " + _ON_ERROR, "",
    ]


def _example_section() -> List[str]:
    return [
        "## Minimal complete example (offline-runnable)", "",
        "Root config (`run.local.json`):", "",
        "```json", json.dumps(_EXAMPLE_ROOT, indent=1), "```", "",
        "Pipeline (`pipeline.json`) — agent, JSON validator, render:", "",
        "```json", json.dumps(_EXAMPLE_PIPELINE, indent=1), "```", "",
    ]


def build_manual() -> str:
    parts = [_HEADER]
    parts.extend(_node_section())
    parts.extend(_root_section())
    parts.extend(_pipeline_section())
    parts.append(_TRANSFORM_CONVENTIONS)
    parts.append(_FOREACH)
    parts.append(_PLACEHOLDERS)
    parts.append(_FAKE_SCRIPTED)
    parts.append(_RULES)
    parts.extend(_example_section())
    parts.append(_FOOTER)
    return "\n".join(parts)
