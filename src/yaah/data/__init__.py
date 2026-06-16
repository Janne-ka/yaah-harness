"""yaah.data — the DataSource / DataSink PORTS + their Routing composer. The
I/O-bound sources/sinks (file, git_diff, file_sink) are swap-in adapters in
yaah.adapters.data. Optional layer, not the kernel.

A DataSource fetches a slice of data by key (a worktree diff scoped to changed
lines, a file's line range, a cloud blob) so a stage reads only what it needs.
"""
from .data_sink import DataSink
from .data_source import DataSource
from .routing_data_sink import RoutingDataSink
from .routing_data_source import RoutingDataSource

__all__ = ["DataSource", "DataSink", "RoutingDataSource", "RoutingDataSink"]
