"""Node — the one interface every worker implements.

Used by: the harness and Comms (they depend ONLY on this, never on a concrete
node). Implemented by: agents, validators, gates, shell/render/python nodes,
and anything else that does a unit of work.
Where: the contract at the centre of the system.
Why: one uniform, opaque shape — invoke(input, config) -> output — so any node
is interchangeable and the harness stays decoupled from what a node does.

Targets Python 3.9+.
"""
from __future__ import annotations

from abc import abstractmethod
from typing import Protocol, runtime_checkable

from .envelope import Envelope
from .node_config import NodeConfig


@runtime_checkable
class Node(Protocol):
    # @abstractmethod so a class that DECLARES `class X(Node)` can't instantiate
    # without invoke(); structural conformance still holds for a non-declaring impl.
    @abstractmethod
    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        ...
