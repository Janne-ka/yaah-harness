"""yaah.agents — the generic Agent worker + the ApiProvider port and its
zero-dependency reference backends (Fake / Scripted / Routing). The external
model providers (claude_cli, litellm) are swap-in adapters in
yaah.adapters.providers. One class per file; re-exported for convenience.

Every provider implements ApiProvider (`stream()`) natively and DECLARES it
in its class header; tool-capable providers also declare SupportsTurn.
"""
from .agent import Agent
from .api_provider import ApiProvider
from .fake_provider import FakeProvider
from .routing_provider import RoutingProvider
from .scripted_provider import ScriptedProvider
from .scripted_tool_provider import ScriptedToolProvider
from .tool import Tool
from .tool_loop import run_tool_loop
from .context_broker_tool import make_context_broker_tool
from .envelope_tool import make_envelope_get_tool

__all__ = [
    "ApiProvider",
    "Agent",
    "FakeProvider",
    "ScriptedProvider",
    "ScriptedToolProvider",
    "RoutingProvider",
    "Tool",
    "run_tool_loop",
    "make_envelope_get_tool",
    "make_context_broker_tool",
]
