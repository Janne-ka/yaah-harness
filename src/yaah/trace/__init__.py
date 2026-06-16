"""yaah.trace — the Tracer + TraceContributor PORTS, the Span record contract,
and the no-external-system carriages (Null / Recording / Bus / Envelope). The
concrete capture modules (phase/cost/tools) and every TraceSink (file /
Langfuse / OTLP) are swap-in adapters in yaah.adapters.trace. Optional layer,
not the kernel.
"""
from .bus_tracer import BusTracer
from .carriage_boundary import CarriageBoundaryNode
from .contributor import TraceContributor
from .envelope_tracer import EnvelopeTracer
from .null_tracer import NullTracer
from .record import project
from .recording_tracer import RecordingTracer
from .sink import TraceSink
from .span import Span
from .tracer import Tracer

# Note: the cross-run aggregator (R8) is a consumer/CLI tool, imported on demand
# from yaah.trace.aggregate (kept out of this eager __init__ like yaah.jsonio, so
# `python -m yaah.trace.aggregate` doesn't trip runpy's double-import warning).

__all__ = [
    "Tracer",
    "TraceContributor",
    "TraceSink",
    "Span",
    "NullTracer",
    "RecordingTracer",
    "BusTracer",
    "CarriageBoundaryNode",
    "EnvelopeTracer",
    "project",
]
