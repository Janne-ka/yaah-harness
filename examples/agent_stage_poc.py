"""PoC: one agent stage on YAAH — the worker-factory pattern in miniature.

Shows the worker-factory shape end to end with NO external services:
  - the generic Agent wrapper (yaah.agents) calling a swappable model backend;
  - a deterministic JSON/schema validator (the "your output isn't JSON" gate);
  - the harness retry-with-feedback loop;
  - event-mode progress (push) to a printing subscriber.

Runs on InProcessComms with FakeProvider — no Dapr, no NATS, no network.
To run against real local Claude, swap one line (see BACKEND below): that uses
ClaudeCliProvider (`claude -p`). LiteLLMProvider is another drop-in.

Run: cd yaah && PYTHONPATH=src python3 examples/agent_stage_poc.py
"""
from __future__ import annotations

import asyncio
import json
from typing import List

from yaah import (
    Done,
    Envelope,
    Failure,
    Graph,
    Harness,
    InProcessComms,
    NodeConfig,
    Stage,
    Verdict,
)
from yaah.agents import Agent, FakeProvider, RoutingProvider
from yaah.jsonio import extract_json  # fence-tolerant — real models wrap JSON in markdown

# from yaah.agents import ClaudeCliProvider, LiteLLMProvider  # real backends


class JsonObjectValidator:
    """Passes if payload['raw'] parses as a JSON object with the required keys."""

    def __init__(self, required: List[str]) -> None:
        self._required = required

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        raw = input.payload.get("raw", "")
        try:
            obj = extract_json(raw)
        except json.JSONDecodeError as e:
            return Verdict.failed(Failure(
                "not_json", "output is not valid JSON: {}".format(e),
                "return a single JSON object")).to_envelope()
        if not isinstance(obj, dict):
            return Verdict.failed(Failure(
                "not_object", "top level is not a JSON object", "return a JSON object")).to_envelope()
        missing = [k for k in self._required if k not in obj]
        if missing:
            return Verdict.failed(Failure(
                "missing_keys", "missing keys: {}".format(missing),
                "include keys {}".format(self._required))).to_envelope()
        return Verdict.passed().to_envelope()


SPEC_PROMPT = """You are a spec worker. Given the task, return a JSON object
with keys: summary, items.

Task: {{task}}
"""

# One backend that routes by the model string's 'provider:' prefix, so a node
# chooses its backend purely via config (NodeConfig.model) — no code change.
BACKEND = RoutingProvider(
    {
        "fake": FakeProvider(responses=[
            '{"summary": "two issues found", "items": ["a", "b"  ',   # invalid → triggers retry
            '{"summary": "two issues found", "items": ["a", "b"]}',   # valid
        ]),
        # "claude": ClaudeCliProvider(),    # real local Claude (`claude -p`, MCP stripped)
        # "litellm": LiteLLMProvider(),     # any provider via litellm (pip install litellm + key)
    },
    default="fake",
)

# The node's backend is selected by this model string — config, not code:
#   "fake:spec"                 -> FakeProvider
#   "claude:claude-sonnet-4-6"  -> ClaudeCliProvider   (uncomment "claude" above)
#   "litellm:gpt-4o"            -> LiteLLMProvider
NODE_MODEL = "fake:spec"


async def main() -> None:
    comms = InProcessComms()

    async def on_event(e: Envelope) -> None:
        print("[event] {}: {}".format(e.payload.get("stage"), e.payload.get("msg")))

    comms.subscribe("events", on_event)

    comms.register(
        "role:spec",
        Agent(BACKEND, SPEC_PROMPT, events=comms, stage="spec", parse=False),
        NodeConfig(model=NODE_MODEL, effort="low"),
    )
    comms.register("role:json", JsonObjectValidator(required=["summary", "items"]))

    graph = Graph.of(
        Stage("spec", node="role:spec", validators=["role:json"], max_attempts=3, feedback=True)
    )

    print("--- one-stage pipeline: Agent + json gate + retry loop ---")
    outcome = await Harness(comms, graph).run(Envelope("task", {"task": "review the diff"}))

    assert isinstance(outcome, Done), outcome
    artifact = extract_json(outcome.output.payload["raw"])
    assert artifact["summary"] and artifact["items"], artifact
    print("DONE artifact:", json.dumps(artifact))
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
