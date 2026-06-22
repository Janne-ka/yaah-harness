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
        '    "role:render":    {"type": "render", "template_file": "templates/output.html",\n'
        '                       "out": "summary.html"}\n'
        '  },\n'
        '  "graph": {\n'
        '    "start": "summarize",\n'
        '    "stages": {\n'
        '      "summarize": {"node": "role:summarize",\n'
        '                    "max_attempts": 3, "feedback": true, "then": "render"},\n'
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
        '    "role:review":  {"type": "human_gate", "form": "approve_or_revise",\n'
        '                     "awaiting": "human:review",\n'
        '                     "ask": "Review the draft:\\n\\n  {{summary}}\\n\\nApprove to publish, or revise to redraft."},\n'
        '    "role:publish": {"type": "render", "template_file": "templates/published.html",\n'
        '                     "out": "published.html"}\n'
        '  },\n'
        '  "graph": {\n'
        '    "start": "draft",\n'
        '    "stages": {\n'
        '      "draft":   {"node": "role:draft",\n'
        '                  "max_attempts": 3, "feedback": true, "then": "review"},\n'
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
        'A `reduce` target gets the arrived branches as `{branch_id: payload}`.\n'
        'Each lens is an agent with parse=True (the default, ADR-0004) so its\n'
        'output JSON has already been merged onto the per-branch payload — the\n'
        'reducer reads `finding` directly. The engine never learns the data\n'
        'shape; the reduce owns it (that is why it is an `fn:` target, not\n'
        'engine code).\n'
        '"""\n'
        '\n'
        '\n'
        'def merge(arrived):\n'
        '    lines = []\n'
        '    for branch in sorted(arrived):\n'
        '        finding = arrived[branch].get("finding", "(unreadable)")\n'
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


# One-line descriptions surfaced by `yaah scaffold --list`. Kept here next to
# ARCHETYPES so a new entry is visibly incomplete until both dicts get the
# new key (the test in test_init_template asserts the two stay in sync).
ARCHETYPE_DESCRIPTIONS: Dict[str, str] = {
    "linear":           "one stage after another (agent → render). Use for: smoke tests, demos, single-shot transforms.",
    "branch-with-gate": "a stage that branches on a verdict, with a human-decision gate parking the run mid-way. Use for: review/approve flows.",
    "fork-fanin":       "fan out to N parallel branches, fan in to one reducer. Use for: candidates × verdict, scout/prefetch/act.",
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
