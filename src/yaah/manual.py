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
    "baton_ttl": "seconds a parked human-gate baton stays claimable",
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
    parts.append(_RULES)
    parts.extend(_example_section())
    parts.append(_FOOTER)
    return "\n".join(parts)
