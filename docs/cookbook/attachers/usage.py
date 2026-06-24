"""Reference `usage` attacher — surfaces token counts and model from the
tracer's most recent `model_call` span into the in-flight payload under
the key `usage`.

This is the canonical implementation of the pattern described in
ADR-0003. Copy this class into your project's transforms module (or any
importable Python file) and reference it from an agent node:

    "role:my-agent": {
      "type": "agent",
      "model": "claude:claude-haiku-4-5",
      "attach": ["fn:transforms:UsageAttacher"]
    }

You ALSO need the matching tracer capture in your root config:

    "trace": { "capture": ["cost"] }

If `cost` is missing from `trace.capture`, the builder rejects the
config at LOAD time with a clear message — the engine never lets you
ship an attacher whose data wasn't captured.

DESIGN NOTE — dollar cost intentionally not here. The tracer's `cost`
contributor projects `{tokens_in, tokens_out, model}`. Dollar pricing
is contract-specific and changes; yaah keeps it in the aggregator's
price-map (see `src/yaah/trace/contributors/cost.py`) so historical
trace data can be re-priced. The A/B variant of `examples/arch-drift`
shows the downstream pattern: a `prices` map in the pipeline config,
multiplied against the attached `usage` numbers in a later transform.

This file is intentionally NOT importable from anywhere in the engine.
It exists only as a copy-paste reference; see `docs/cookbook/README.md`
for the rationale.
"""
from yaah.agents.attacher import Attacher


class UsageAttacher(Attacher):
    name = "usage"
    requires_capture = ("cost",)

    def attach(self, envelope, span):
        if not span:
            return {}
        return {"usage": {
            "tokens_in": span.get("tokens_in", 0),
            "tokens_out": span.get("tokens_out", 0),
            "model": span.get("model"),
        }}
