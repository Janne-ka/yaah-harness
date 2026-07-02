"""THE port-declaration contract, in one place: every shipped impl DECLARES its
port in the class header (AGENTS.md "Declare your port"), and a declared-but-
incomplete subclass fails at instantiation.

Why `__mro__` and not isinstance/issubclass: the ports are @runtime_checkable
Protocols, so isinstance is STRUCTURAL — it passes for any class of the right
shape and proves nothing about the declaration. `Port in cls.__mro__` is real
inheritance, so removing a declaration from a class header fails here.

Adding a port or impl = one row below.

Run: cd yaah && PYTHONPATH=src python3 tests/test_ports.py
"""
from __future__ import annotations

from yaah.agents import (
    ApiProvider, FakeProvider, RoutingProvider, ScriptedProvider, ScriptedToolProvider,
)
from yaah.agents.api_provider import SupportsTurn
from yaah.agents.agent import Agent
from yaah.agents.attaching_agent import AttachingAgent
from yaah.adapters.providers import ClaudeCliProvider, FakeToolProvider, LiteLLMProvider
from yaah.adapters.stores import FileBackend
from yaah.adapters.trace import (
    ConsoleTraceSink, FileTraceSink, LangfuseTraceSink, ProgressFileSink, StatsFileSink,
)
from yaah.adapters.transports import LocalBus
from yaah.adapters.transports.nats_comms import NatsComms, _NatsSubscription
from yaah.build.human_gate import HumanGate
from yaah.build.live_config_node import LiveConfigNode
from yaah.comms import Comms, InMemorySubscription, InProcessComms, Subscription
from yaah.core import Node
from yaah.harness import BatonStore
from yaah.nodes import (
    AgentLoopNode, GetNode, OnceNode, PostNode, RenderNode, ShellCheck, ShellNode,
    TransformNode, WorktreeNode,
)
from yaah.store import (
    CompareAndSet, EnvelopeStore, IdempotencyStore, MemoryBackend, Scannable,
    StoreBackedFacade, StoreBackend,
)
from yaah.trace import (
    BusTracer, CarriageBoundaryNode, EnvelopeTracer, NullTracer, RecordingTracer,
    TraceContributor, TraceSink, Tracer,
)
from yaah.trace.contributors import CostContributor, PhaseContributor, ToolsContributor
from yaah.validators import ExpectField, JsonObjectValidator, JsonSchemaValidator

# port -> every shipped impl that must declare it.
PORTS = {
    Node: [Agent, AttachingAgent, HumanGate, LiveConfigNode, AgentLoopNode, GetNode,
           OnceNode, PostNode, RenderNode, ShellCheck, ShellNode, TransformNode,
           WorktreeNode, CarriageBoundaryNode, ExpectField, JsonObjectValidator,
           JsonSchemaValidator],
    ApiProvider: [FakeProvider, ScriptedProvider, ScriptedToolProvider, FakeToolProvider,
                  ClaudeCliProvider, LiteLLMProvider, RoutingProvider],
    SupportsTurn: [ScriptedToolProvider, FakeToolProvider, LiteLLMProvider, RoutingProvider],
    StoreBackend: [MemoryBackend, FileBackend],
    Scannable: [MemoryBackend, FileBackend],
    CompareAndSet: [MemoryBackend, FileBackend],
    StoreBackedFacade: [EnvelopeStore, IdempotencyStore, BatonStore],
    Comms: [InProcessComms, LocalBus, NatsComms],
    Subscription: [InMemorySubscription, _NatsSubscription],
    Tracer: [BusTracer, EnvelopeTracer, NullTracer, RecordingTracer],
    TraceSink: [ConsoleTraceSink, FileTraceSink, LangfuseTraceSink, ProgressFileSink,
                StatsFileSink],
    TraceContributor: [CostContributor, PhaseContributor, ToolsContributor],
}

# Ports whose abstract methods make an empty declared subclass unbuildable.
# (StoreBackedFacade is a concrete base, not a Protocol — excluded.)
ENFORCED = [Node, ApiProvider, SupportsTurn, StoreBackend, Scannable, CompareAndSet,
            Comms, Subscription, Tracer, TraceSink, TraceContributor]

# claude_cli deliberately has NO native turn() (it runs its own tool loop) —
# the capability must read as ABSENT both nominally and structurally.
NOT_PORTS = {SupportsTurn: [ClaudeCliProvider, FakeProvider, ScriptedProvider]}


def test_impls_declare_their_ports() -> None:
    for port, impls in PORTS.items():
        for cls in impls:
            assert port in cls.__mro__, "{} must declare {}".format(
                cls.__name__, port.__name__)


def test_deliberate_non_ports_stay_absent() -> None:
    for port, impls in NOT_PORTS.items():
        for cls in impls:
            assert port not in cls.__mro__, "{} must NOT declare {}".format(
                cls.__name__, port.__name__)


def test_incomplete_declared_subclass_cannot_instantiate() -> None:
    for port in ENFORCED:
        half = type("Half" + port.__name__, (port,), {})  # declares, implements nothing
        try:
            half()
        except TypeError:
            pass
        else:
            raise AssertionError(
                "incomplete {} subclass instantiated".format(port.__name__))


if __name__ == "__main__":
    test_impls_declare_their_ports()
    test_deliberate_non_ports_stay_absent()
    test_incomplete_declared_subclass_cannot_instantiate()
    print("ok")
