"""YAAH — Yet Another Agentic Harness. Generic worker-orchestration kernel.

The whole kernel is three concepts (see yaah/docs/design.md):
  Envelope - one message shape.
  Node     - invoke(input, config) -> output. Every worker, agents included.
  Comms    - request / publish / subscribe. The only thing the harness calls.
"""
from .adapters.transports import LocalBus, NatsComms
from .comms import Comms, Handler, InProcessComms, Subscription
from .core import Envelope, Failure, Kind, Node, NodeConfig, Verdict
from .harness import (
    Baton,
    Cleared,
    Decider,
    Done,
    Graph,
    Harness,
    Stage,
    StageFailed,
    Suspended,
    drive,
)

__all__ = [
    # kernel
    "Envelope",
    "Kind",
    "NodeConfig",
    "Verdict",
    "Failure",
    "Node",
    "Comms",
    "Handler",
    "Subscription",
    "InProcessComms",
    "LocalBus",
    "NatsComms",
    # harness
    "Harness",
    "Graph",
    "Stage",
    "Baton",
    "Cleared",
    "Done",
    "Suspended",
    "StageFailed",
    "drive",
    "Decider",
]
