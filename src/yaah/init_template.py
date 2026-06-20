"""Embedded scaffold templates for the `yaah init` and `yaah scaffold` subcommands.

A fresh user runs `pip install yaah-harness && yaah init my-pipeline` and gets a
runnable `agent -> validate -> parse -> render` pipeline on fake providers — no
repo checkout required. The `yaah scaffold <archetype> <dir>` form lets them
pick which archetype to start from; `init` defaults to `linear` (the
hello-yaah shape) for back-compat and quick-start ergonomics.

Each template mirrors a corresponding `examples/<name>/` in spirit, but is
intentionally SMALLER — a minimal working version users adapt, not the full
production-shape example. For the `linear` template specifically,
`tests/test_init_template.py` keeps it byte-for-byte in sync with
`examples/hello-yaah/` so doc-and-template drift is caught at suite time.
Other archetypes' templates just have to validate + run; they're not
required to mirror their reference example exactly.

Called by `yaah.runtime._dispatch` when `spec["action"] == "init"` or
`spec["action"] == "scaffold"`. See `docs/archetypes.md` for what each
archetype is for.
"""
from __future__ import annotations

import os
from typing import Dict


# ---------- linear (hello-yaah) ----------------------------------------------

LINEAR_TEMPLATE: Dict[str, str] = {
    "starter.json": (
        '{\n'
        '  "nodes": {\n'
        '    "role:summarize": {"type": "agent", "prompt": "file:summarize",\n'
        '                       "model": "fake:summarize", "stage": "summarize"},\n'
        '    "role:check":     {"type": "json_object", "required": ["summary"]},\n'
        '    "role:parse":     {"type": "transform", "target": "fn:hello_transforms:parse",\n'
        '                       "call": "envelope"},\n'
        '    "role:render":    {"type": "render", "template_file": "templates/output.html",\n'
        '                       "out": "summary.html"}\n'
        '  },\n'
        '  "graph": {\n'
        '    "start": "summarize",\n'
        '    "stages": {\n'
        '      "summarize": {"node": "role:summarize", "validators": ["role:check"],\n'
        '                    "max_attempts": 3, "feedback": true, "then": "parse"},\n'
        '      "parse":     {"node": "role:parse", "then": "render"},\n'
        '      "render":    {"node": "role:render", "then": null}\n'
        '    }\n'
        '  }\n'
        '}\n'
    ),
    "starter.local.json": (
        '{\n'
        '  "transport": {"type": "inproc"},\n'
        '  "providers": {"fake": {"type": "fake_scripted",\n'
        '                         "by_model": {"summarize": ["{\\"summary\\":\\"hello\\"}"]}}},\n'
        '  "default_provider": "fake",\n'
        '  "prompt_sources": {"file": {"type": "file", "dir": "prompts"}},\n'
        '  "default_prompt_source": "file",\n'
        '  "state": {"type": "memory"},\n'
        '  "pipeline": "starter.json",\n'
        '  "input": "fixtures/input.json",\n'
        '  "run": true\n'
        '}\n'
    ),
    "hello_transforms.py": (
        '"""The parse transform for the hello-yaah pipeline.\n'
        '\n'
        'A `transform` node with `call: "envelope"` is `fn(envelope, config) -> dict`; the\n'
        'returned dict SPREADS over the payload top-level. That is how `summary` becomes a\n'
        'real payload key the `render` stage can interpolate — an agent\'s output arrives as\n'
        'a STRING in `payload["raw"]`, and nothing merges it until a parse step like this.\n'
        '"""\n'
        'from yaah.jsonio import extract_json\n'
        '\n'
        '\n'
        'def parse(envelope, config):\n'
        '    # extract_json (not json.loads) — sonnet/haiku wrap JSON in markdown\n'
        '    # fences; strict json.loads breaks on real-model runs. opus is the\n'
        '    # only model that reliably emits bare JSON.\n'
        '    return extract_json(envelope.payload.get("raw", "{}"))\n'
    ),
    "prompts/summarize.md": (
        'Summarize {{text}} in one sentence. Return JSON: {"summary": "..."}\n'
    ),
    "templates/output.html": (
        '<h1>{{summary}}</h1>\n'
    ),
    "fixtures/input.json": (
        '{"text": "YAAH is a domain-free harness."}\n'
    ),
    ".gitignore": (
        'summary.html\n'
    ),
}


# Back-compat alias — tests/test_init_template.py + downstream code that
# imported STARTER_TEMPLATE before scaffold landed.
STARTER_TEMPLATE = LINEAR_TEMPLATE


# ---------- branch-with-gate -------------------------------------------------

BRANCH_WITH_GATE_TEMPLATE: Dict[str, str] = {
    "starter.json": (
        '{\n'
        '  "nodes": {\n'
        '    "role:draft":   {"type": "agent", "prompt": "file:draft",\n'
        '                     "model": "fake:draft", "stage": "draft"},\n'
        '    "role:check":   {"type": "json_object", "required": ["summary"]},\n'
        '    "role:parse":   {"type": "transform", "target": "fn:transforms:parse",\n'
        '                     "call": "envelope"},\n'
        '    "role:review":  {"type": "human_gate", "form": "approve_or_revise",\n'
        '                     "awaiting": "human:review",\n'
        '                     "ask": "Review the draft:\\n\\n  {{summary}}\\n\\nApprove to publish, or revise to redraft."},\n'
        '    "role:publish": {"type": "render", "template_file": "templates/published.html",\n'
        '                     "out": "published.html"}\n'
        '  },\n'
        '  "graph": {\n'
        '    "start": "draft",\n'
        '    "stages": {\n'
        '      "draft":   {"node": "role:draft", "validators": ["role:check"],\n'
        '                  "max_attempts": 3, "feedback": true, "then": "parse"},\n'
        '      "parse":   {"node": "role:parse", "then": "review"},\n'
        '      "review":  {"node": "role:review",\n'
        '                  "branch": {"on": "decision",\n'
        '                             "routes": {"revise": "draft"},\n'
        '                             "default": "publish"}},\n'
        '      "publish": {"node": "role:publish", "then": null}\n'
        '    }\n'
        '  }\n'
        '}\n'
    ),
    "starter.local.json": (
        '{\n'
        '  "_about": "starter for the branch-with-gate archetype. The state store is FILE-backed because human gates suspend across processes.",\n'
        '  "transport": {"type": "inproc"},\n'
        '  "providers": {"fake": {"type": "fake_scripted",\n'
        '                         "by_model": {"draft": ["{\\"summary\\":\\"draft v1\\"}"]}}},\n'
        '  "default_provider": "fake",\n'
        '  "prompt_sources": {"file": {"type": "file", "dir": "prompts"}},\n'
        '  "default_prompt_source": "file",\n'
        '  "state": {"type": "file", "dir": "state"},\n'
        '  "pipeline": "starter.json",\n'
        '  "input": "fixtures/input.json",\n'
        '  "run": true\n'
        '}\n'
    ),
    "transforms.py": (
        '"""Parse the draft agent\'s JSON string into payload keys.\n'
        '\n'
        'Pattern: an agent\'s output is a STRING in payload["raw"] until a transform\n'
        'like this merges it. See docs/archetypes.md (linear) for the full data-flow\n'
        'contract; branch-with-gate is the same pattern + a human gate downstream.\n'
        '"""\n'
        'from yaah.jsonio import extract_json\n'
        '\n'
        '\n'
        'def parse(envelope, config):\n'
        '    return extract_json(envelope.payload.get("raw", "{}"))\n'
    ),
    "prompts/draft.md": (
        'Draft a one-sentence summary of: {{text}}\n'
        '\n'
        'Return JSON: {"summary": "..."}\n'
    ),
    "templates/published.html": (
        '<h1>Published</h1>\n'
        '<p>{{summary}}</p>\n'
    ),
    "fixtures/input.json": (
        '{"text": "YAAH routes agents; a human reviews; the harness keeps state."}\n'
    ),
    "decision.json": (
        '{"decision": "approve"}\n'
    ),
    ".gitignore": (
        'state/\n'
        'published.html\n'
    ),
    "README.md": (
        '# starter (branch-with-gate)\n'
        '\n'
        'Scaffolded from the `branch-with-gate` archetype. The pipeline drafts a\n'
        'summary, parks for human review, then either publishes (approve) or\n'
        're-drafts (revise).\n'
        '\n'
        '## Run\n'
        '\n'
        '```bash\n'
        '# Start the run; the human gate suspends.\n'
        'python3 -m yaah.runtime starter.local.json\n'
        '\n'
        '# See what is parked.\n'
        'yaah list starter.local.json\n'
        '\n'
        '# Deliver the decision (the file decision.json has {"decision": "approve"}).\n'
        'yaah resume starter.local.json <baton-id> decision.json\n'
        '```\n'
        '\n'
        '## Adapt\n'
        '\n'
        '- Change the prompt in `prompts/draft.md`.\n'
        '- Change the decision shape: the gate uses `form: "approve_or_revise"`\n'
        '  (see `docs/decision-forms.md`); swap to `free_text` or `json_schema` if\n'
        '  the operator needs to provide structured revision content.\n'
        '- Add more branch routes by adding entries to the `routes:` map.\n'
        '- For the real provider, copy `starter.local.json` to `starter.real.json`,\n'
        '  set `_extends: "starter.local.json"`, and swap `providers.fake` for\n'
        '  `providers.claude` (with `by_model: null` to delete the inherited stub).\n'
        '\n'
        '## Reference\n'
        '\n'
        '- `examples/review-pipeline/` in the yaah repo — fuller version of this shape.\n'
        '- `docs/archetypes.md` — what makes this archetype distinct.\n'
        '- `docs/decision-forms.md` — gate decision shapes.\n'
    ),
}


# ---------- fork-fanin -------------------------------------------------------

FORK_FANIN_TEMPLATE: Dict[str, str] = {
    "starter.json": (
        '{\n'
        '  "nodes": {\n'
        '    "role:lens-a": {"type": "agent", "model": "fake:lens-a", "stage": "lens-a",\n'
        '                    "template": "Review {{text}} from PERSPECTIVE A. Return JSON: {\\"finding\\": \\"...\\"}"},\n'
        '    "role:lens-b": {"type": "agent", "model": "fake:lens-b", "stage": "lens-b",\n'
        '                    "template": "Review {{text}} from PERSPECTIVE B. Return JSON: {\\"finding\\": \\"...\\"}"},\n'
        '    "role:lens-c": {"type": "agent", "model": "fake:lens-c", "stage": "lens-c",\n'
        '                    "template": "Review {{text}} from PERSPECTIVE C. Return JSON: {\\"finding\\": \\"...\\"}"},\n'
        '    "role:report": {"type": "render", "out": "report.html",\n'
        '                    "template_text": "<h1>Report — {{count}} lenses</h1>\\n<pre>{{report}}</pre>\\n"}\n'
        '  },\n'
        '  "graph": {\n'
        '    "start": "spread",\n'
        '    "stages": {\n'
        '      "spread": {"node": "", "fork": ["lens-a", "lens-b", "lens-c"], "then": "report"},\n'
        '      "lens-a": {"node": "role:lens-a", "then": "join"},\n'
        '      "lens-b": {"node": "role:lens-b", "then": "join"},\n'
        '      "lens-c": {"node": "role:lens-c", "then": "join"},\n'
        '      "join":   {"node": "",\n'
        '                 "fanin": {"expect": ["lens-a", "lens-b", "lens-c"], "wait": "all",\n'
        '                           "reduce": "fn:transforms:merge"},\n'
        '                 "then": null},\n'
        '      "report": {"node": "role:report", "then": null}\n'
        '    }\n'
        '  }\n'
        '}\n'
    ),
    "starter.local.json": (
        '{\n'
        '  "transport": {"type": "inproc"},\n'
        '  "providers": {"fake": {"type": "fake_scripted",\n'
        '                         "by_model": {\n'
        '                            "lens-a": ["{\\"finding\\":\\"finding from lens A\\"}"],\n'
        '                            "lens-b": ["{\\"finding\\":\\"finding from lens B\\"}"],\n'
        '                            "lens-c": ["{\\"finding\\":\\"finding from lens C\\"}"]}}},\n'
        '  "default_provider": "fake",\n'
        '  "state": {"type": "memory"},\n'
        '  "pipeline": "starter.json",\n'
        '  "input": "fixtures/input.json",\n'
        '  "run": true\n'
        '}\n'
    ),
    "transforms.py": (
        '"""The fan-in reduce for the fork-fanin starter.\n'
        '\n'
        'A `reduce` target gets the arrived branches as `{branch_id: payload}` — each\n'
        'lens left its JSON finding in `payload["raw"]`. We merge them into one\n'
        'report. The engine never learns the data shape; the reduce owns it (that is\n'
        'why it is an `fn:` target, not engine code).\n'
        '"""\n'
        'from yaah.jsonio import extract_json\n'
        '\n'
        '\n'
        'def merge(arrived):\n'
        '    lines = []\n'
        '    for branch in sorted(arrived):\n'
        '        raw = arrived[branch].get("raw", "{}")\n'
        '        try:\n'
        '            finding = extract_json(raw).get("finding", "")\n'
        '        except Exception:\n'
        '            finding = "(unreadable)"\n'
        '        lines.append("- [{}] {}".format(branch, finding))\n'
        '    return {"report": "\\n".join(lines), "count": len(lines)}\n'
    ),
    "fixtures/input.json": (
        '{"text": "Review me from three angles."}\n'
    ),
    ".gitignore": (
        'report.html\n'
    ),
    "README.md": (
        '# starter (fork-fanin)\n'
        '\n'
        'Scaffolded from the `fork-fanin` archetype. Three independent agents (lenses)\n'
        'review the same input in parallel; a reducer merges their findings into one\n'
        'report.\n'
        '\n'
        '## Run\n'
        '\n'
        '```bash\n'
        'python3 -m yaah.runtime starter.local.json\n'
        '# → produces report.html with all three lenses\' findings\n'
        '```\n'
        '\n'
        '## Adapt\n'
        '\n'
        '- Add or remove lenses: every lens has THREE matching places — a `nodes:`\n'
        '  entry, a stage in `graph.stages`, and an entry in `fork:` AND in\n'
        '  `fanin.expect`. The `fanin.expect` list takes FORK BRANCH names (not the\n'
        '  names of the last stage in each branch).\n'
        '- Change the reduce logic in `transforms.py:merge`. It receives\n'
        '  `{branch_id: payload}`; return a dict that spreads onto the next stage\'s\n'
        '  input.\n'
        '- For the real provider, copy `starter.local.json` to `starter.real.json`,\n'
        '  set `_extends: "starter.local.json"`, and swap `providers.fake` for\n'
        '  `providers.claude` (with `by_model: null` to delete the inherited stub).\n'
        '\n'
        '## Reference\n'
        '\n'
        '- `examples/fork-join/` in the yaah repo — fuller version of this shape.\n'
        '- `docs/archetypes.md` — what makes this archetype distinct.\n'
    ),
}


# ---------- archetype registry -----------------------------------------------

ARCHETYPES: Dict[str, Dict[str, str]] = {
    "linear":           LINEAR_TEMPLATE,
    "branch-with-gate": BRANCH_WITH_GATE_TEMPLATE,
    "fork-fanin":       FORK_FANIN_TEMPLATE,
    # Future: "instrumented" and "meta-tool" archetypes when their templates
    # are written. See docs/archetypes.md for what each shape is for. The
    # scaffold dispatch already supports adding entries here; the gating
    # work is template design + tests, not the runtime.
}


def scaffold(target_dir: str, archetype: str = "linear") -> int:
    """Write the named archetype's template into `target_dir`. Returns file count.

    Refuses to overwrite if `target_dir` already exists and is non-empty —
    `yaah init` / `yaah scaffold` must never silently clobber a user's work.

    Raises ValueError if the archetype is unknown — the message lists the
    known names so the operator (or agent) can self-correct."""
    if archetype not in ARCHETYPES:
        raise ValueError(
            "unknown archetype {!r}; known: {} — see `docs/archetypes.md` "
            "for what each shape is for".format(archetype, sorted(ARCHETYPES)))
    if os.path.exists(target_dir) and os.listdir(target_dir):
        raise FileExistsError(
            "{!r} exists and is not empty — refusing to overwrite".format(target_dir))
    template = ARCHETYPES[archetype]
    for relpath, content in template.items():
        path = os.path.join(target_dir, relpath)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
    return len(template)
