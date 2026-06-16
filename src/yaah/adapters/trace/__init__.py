"""Trace sinks (adapters). Destinations for the trace record stream, each
implementing the TraceSink port (which stays in yaah.trace). The engine emits
spans; these persist/forward them: file (JSONL), console (stderr progress),
Langfuse (the prompt-store's write-side twin). Add a sink here + a runtime
factory-map entry; the engine and pipelines are untouched.
"""
from .console_trace_sink import ConsoleTraceSink
from .file_trace_sink import FileTraceSink
from .langfuse_trace_sink import LangfuseTraceSink
from .progress_file_sink import ProgressFileSink
from .stats_file_sink import StatsFileSink

__all__ = ["FileTraceSink", "ConsoleTraceSink", "LangfuseTraceSink",
           "ProgressFileSink", "StatsFileSink"]
