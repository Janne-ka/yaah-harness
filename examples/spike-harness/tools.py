"""Demo dispatch fns for the spike-harness example.

These are plain Python — the same `fn:module:func` resolver
transforms use. The agent_loop calls them via the dispatch callable
declared in pipeline.json's tools[name].dispatch field.
"""
from __future__ import annotations

import os


async def read_file(args):
    path = args.get("path", "")
    full = os.path.join(os.path.dirname(__file__), path)
    if not os.path.isfile(full):
        return "file not found: {}".format(path)
    with open(full, "r", encoding="utf-8") as fh:
        return fh.read()


async def done(args):
    # Signal-only — the loop sees this as a tool result and the agent's
    # next turn typically emits the final text.
    return "noted: {}".format(args.get("summary", ""))
