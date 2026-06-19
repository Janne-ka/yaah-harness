"""Embedded starter template for the `yaah init <dir>` subcommand.

A fresh user runs `pip install yaah-harness && yaah init my-pipeline` and gets a
runnable `agent -> validate -> parse -> render` pipeline on fake providers — no
repo checkout required. The template mirrors `examples/hello-yaah/`; the test
`tests/test_init_template.py` keeps the two in sync so doc-and-template drift is
caught at suite time, not by a confused user.

Called by `yaah.runtime._dispatch` when `spec["action"] == "init"`. Usability
gaps #1 / §3: discoverability of the CLI hinges on `init` existing.
"""
from __future__ import annotations

import os
from typing import Dict


STARTER_TEMPLATE: Dict[str, str] = {
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
        'import json\n'
        '\n'
        '\n'
        'def parse(envelope, config):\n'
        '    return json.loads(envelope.payload.get("raw", "{}"))\n'
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


def scaffold(target_dir: str) -> int:
    """Write the starter template into `target_dir`. Returns file count. Refuses
    to overwrite if `target_dir` already exists and is non-empty — `yaah init`
    must never silently clobber a user's work."""
    if os.path.exists(target_dir) and os.listdir(target_dir):
        raise FileExistsError(
            "{!r} exists and is not empty — refusing to overwrite".format(target_dir))
    for relpath, content in STARTER_TEMPLATE.items():
        path = os.path.join(target_dir, relpath)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
    return len(STARTER_TEMPLATE)
