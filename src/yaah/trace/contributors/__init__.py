"""yaah.trace.contributors — the bundled capture modules (pure projection, no
external system). Each implements the TraceContributor port. `phase` is the
default-on minimum; `cost` and `tools` are opt-in. Compose freely via
`capture: [...]`. A capture that binds to an outside system would instead be an
adapter in yaah.adapters.trace.
"""
from .cost import CostContributor
from .phase import PhaseContributor
from .tools import ToolsContributor

# name -> contributor factory, for the runtime to build a capture set from config
BUILTIN_CONTRIBUTORS = {
    "phase": PhaseContributor,
    "cost": CostContributor,
    "tools": ToolsContributor,
}

__all__ = ["PhaseContributor", "CostContributor", "ToolsContributor",
           "BUILTIN_CONTRIBUTORS"]
