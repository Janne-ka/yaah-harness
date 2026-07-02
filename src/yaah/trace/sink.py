"""TraceSink — the port a trace destination implements.

Used by: the runtime subscribes each configured sink to the `trace` topic, so a
BusTracer's published records flow to it. Implemented by: ConsoleTraceSink,
FileTraceSink, LangfuseTraceSink, ProgressFileSink, StatsFileSink (all swap-in
adapters in yaah.adapters.trace).
Where: the engine tracing core defines the PORT; the destinations are adapters
(they bind to a file / stderr / Langfuse — outside systems).
Why: one interface so a destination is config, not code — add a sink, subscribe
it, done. A sink is just a Comms subscriber: it receives the trace Envelope
(record in `.payload`) and persists/renders it however it likes.

Targets Python 3.9+.
"""
from __future__ import annotations

from abc import abstractmethod
from typing import Protocol, runtime_checkable

from ..core import Envelope


@runtime_checkable
class TraceSink(Protocol):
    @abstractmethod
    async def handle(self, env: Envelope) -> None:
        ...
