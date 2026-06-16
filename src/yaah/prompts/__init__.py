"""yaah.prompts — the PromptSource PORT + its zero-config references (Static,
Routing). I/O-bound sources (file, http, langfuse) are swap-in adapters in
yaah.adapters.prompts. Optional layer, not the kernel.
"""
from .prompt_source import PromptSource
from .routing_prompt_source import RoutingPromptSource
from .static_prompt_source import StaticPromptSource

__all__ = ["PromptSource", "StaticPromptSource", "RoutingPromptSource"]
