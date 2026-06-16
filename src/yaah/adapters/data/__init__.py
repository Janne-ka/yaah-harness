"""Data sources/sinks (adapters). I/O-bound implementations of the DataSource /
DataSink ports (which, with the Routing references, stay in yaah.data).
"""
from .file_data_source import FileDataSource
from .file_sink import FileSink
from .git_diff_source import GitDiffSource

__all__ = ["FileDataSource", "FileSink", "GitDiffSource"]
