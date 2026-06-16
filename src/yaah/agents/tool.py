"""Tool — one capability an agent's model may call mid-reasoning.

Used by: the agent tool-loop (and built from an agent node's `tools` config).
Where: agent config — a tool is model-initiated, not a pipeline node (see
docs/agent-tools.md).
Why: carry what the model sees (name/description/schema) plus `impl` — the
transform target (`fn:`/`node:`/`http:`) that actually runs when the model calls
it. So a tool's implementation reuses `call_target`, the same resolver the
`transform` node uses.

Targets Python 3.9+.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class Tool:
    name: str
    impl: Any                                   # a call_target target (fn:/node:/http:) OR a callable(args)->result (a per-invocation handler, e.g. envelope_get bound to the current envelope)
    description: str = ""
    schema: Dict[str, Any] = field(default_factory=dict)  # JSON Schema for the args
    usage: str = ""                             # R11: prompt-side invocation hint rendered into the manifest (e.g. "Run: bash <abs/path/script.sh>"). Empty -> a generic fallback is used. Lets a single Tool spec drive both turn-style (litellm) and prompt-style (claude_cli) backends.

    @classmethod
    def from_dict(cls, spec: Dict[str, Any]) -> "Tool":
        if "name" not in spec or "impl" not in spec:
            raise ValueError("a tool needs 'name' and 'impl', got {!r}".format(spec))
        return cls(name=spec["name"], impl=spec["impl"],
                   description=spec.get("description", ""),
                   schema=spec.get("schema", {}),
                   usage=spec.get("usage", ""))

    def to_function_schema(self) -> Dict[str, Any]:
        """OpenAI/litellm function-calling shape (what the model is shown)."""
        return {"type": "function", "function": {
            "name": self.name,
            "description": self.description,
            "parameters": self.schema or {"type": "object", "properties": {}},
        }}
