"""yaah.build — config-driven construction of nodes + pipeline.

Classes one-per-file (BuildContext, Registry, HumanGate); the builder and
build/serve functions are grouped in `builders` and `build`. Re-exported here so
`from yaah.build import build, serve_from_config, default_registry, ...` keep
working.
"""
from .build import (
    build,
    build_from_json,
    build_graph,
    harness_from_config,
    serve_from_config,
    validate_pipeline,
)
from .build_context import BuildContext
from .builders import default_registry
from .human_gate import HumanGate
from .registry import NodeBuilder, Registry

__all__ = [
    "build",
    "build_graph",
    "harness_from_config",
    "serve_from_config",
    "build_from_json",
    "validate_pipeline",
    "default_registry",
    "BuildContext",
    "Registry",
    "NodeBuilder",
    "HumanGate",
]
