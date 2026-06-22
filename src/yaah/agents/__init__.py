"""yaah.agents — the generic Agent worker + the ApiProvider port and its
zero-dependency reference backends (Fake / Scripted / Routing). The external
model providers (claude_cli, litellm) are swap-in adapters in
yaah.adapters.backends. One class per file; re-exported for convenience.

Post-B6: the legacy ModelBackend / ToolBackend Protocols are gone; every
backend implements ApiProvider natively. Type annotations that named the
old Protocols now use `Any` (or `ApiProvider` when streaming is required).
See .notes/breaking-changes.md for the consumer migration guide.
"""
from .agent import Agent
from .api_provider import ApiProvider
from .fake_backend import FakeBackend
from .routing_backend import RoutingBackend
from .scripted_backend import ScriptedBackend
from .scripted_tool_backend import ScriptedToolBackend
from .tool import Tool
from .tool_loop import run_tool_loop
from .context_broker_tool import make_context_broker_tool
from .envelope_tool import make_envelope_get_tool

__all__ = [
    "ApiProvider",
    "Agent",
    "FakeBackend",
    "ScriptedBackend",
    "ScriptedToolBackend",
    "RoutingBackend",
    "Tool",
    "run_tool_loop",
    "make_envelope_get_tool",
    "make_context_broker_tool",
]
