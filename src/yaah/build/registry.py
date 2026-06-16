"""Registry — maps a node 'type' name to a builder function.

Used by: build() / serve_from_config() (to construct each node from its config);
apps call register() to add their own node types.
Where: the extension point for new node kinds.
Why: keep the set of node types open — config references a type, the registry
knows how to build it.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Callable, Dict

from ..core import Node
from .build_context import BuildContext

NodeBuilder = Callable[[Dict[str, Any], BuildContext], Node]


class Registry:
    def __init__(self) -> None:
        self._builders: Dict[str, NodeBuilder] = {}

    def register(self, type_name: str, builder: NodeBuilder) -> NodeBuilder:
        self._builders[type_name] = builder
        return builder

    def build(self, spec: Dict[str, Any], ctx: BuildContext) -> Node:
        t = spec.get("type")
        if t not in self._builders:
            raise KeyError("unknown node type {!r}; have {}".format(t, sorted(self._builders)))
        return self._builders[t](spec, ctx)
