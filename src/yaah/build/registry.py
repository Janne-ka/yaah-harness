"""Registry — maps a node 'type' name to a builder function.

Used by: build() / serve_from_config() (to construct each node from its config);
apps call register() to add their own node types.
Where: the extension point for new node kinds.
Why: keep the set of node types open — config references a type, the registry
knows how to build it.

A registered type may ALSO carry its data-flow CONTRACT (ADR-0006 D7.1): a pure
`contract(cfg) -> Contract` and/or `consumes(cfg, base_path) -> frozenset`, the same
shape the built-ins use (`node_contract.BUILTIN_CONTRACTS` / `BUILTIN_CONSUMES`). This
is the LIVE binding path: an embedding app that registers a custom node type can now let
the author-time lint SEE that type's contract, so a downstream read of a key the type
cannot provide is caught with ERROR-grade reach — where an unregistered custom type stays
opaque (a sound skip, zero checking). Built-ins keep BUILTIN_CONTRACTS as their source;
the registry's slot holds ONLY custom types, and `contract_source()` chains the two.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from ..core import Node
# node_contract is domain-free and imports only `.templating` — no cycle back to build.
from ..node_contract import (Contract, ConsumesFor, ContractFor, builtin_consumes_for,
                             builtin_contract_for, opaque)
from .build_context import BuildContext
from .macros import expand_macros

NodeBuilder = Callable[[Dict[str, Any], BuildContext], Node]
# A registered contract/consumes matches the built-in fn signatures (node_contract):
#   contract(cfg) -> Contract        consumes(cfg, base_path) -> frozenset
ContractFn = Callable[[Dict[str, Any]], Contract]
ConsumesFn = Callable[[Dict[str, Any], Optional[str]], "frozenset"]


class Registry:
    def __init__(self) -> None:
        self._builders: Dict[str, NodeBuilder] = {}
        # custom-type contracts only — built-ins live in node_contract.BUILTIN_CONTRACTS.
        self._contracts: Dict[str, ContractFn] = {}
        self._consumes: Dict[str, ConsumesFn] = {}

    def register(self, type_name: str, builder: NodeBuilder, *,
                 contract: Optional[ContractFn] = None,
                 consumes: Optional[ConsumesFn] = None) -> NodeBuilder:
        """Register a node type's builder, and OPTIONALLY its data-flow contract/consumes
        (pure `contract(cfg)` / `consumes(cfg, base_path)`; see the module docstring). The
        contract slots are additive — omitting them is exactly today's behaviour (the type
        resolves to opaque/inline at lint time)."""
        self._builders[type_name] = builder
        if contract is not None:
            self._contracts[type_name] = contract
        if consumes is not None:
            self._consumes[type_name] = consumes
        return builder

    def build(self, spec: Dict[str, Any], ctx: BuildContext) -> Node:
        """Construct the node. Path MACROS (`{base_dir}`, `{run_dir}`) are expanded
        on the spec HERE so a builder only ever sees resolved absolute paths and no
        builder has to re-implement the expansion (see build.macros).

        This is NOT the canonical expansion seam — `build._built_nodes` is, because
        the spec is read by more than the builder (`_wrap_node`, `_node_config` →
        `NodeConfig.extras`) and only expanding upstream of all three keeps them
        consistent. The expansion is kept here as well for the DIRECT-EMBEDDER path
        (`registry.build(spec, ctx)` with no `_built_nodes` above it). Running both
        is harmless: `expand_macros` consumes its tokens, so a second pass over an
        already-expanded spec substitutes nothing."""
        t = spec.get("type")
        if t not in self._builders:
            raise KeyError("unknown node type {!r}; have {}".format(t, sorted(self._builders)))
        return self._builders[t](expand_macros(spec, ctx), ctx)

    # --- D7.1: the contract lookup (mirrors builtin_contract_for's never-raise discipline) ---

    def contract_for(self, ntype: Any, cfg: Dict[str, Any]) -> Optional[Contract]:
        """A CUSTOM type's registered contract, or None for an unregistered / contract-less type
        (undeclared → the resolver falls to inline/opaque). Never raises: an unhashable type or a
        malformed cfg yields None; a registered fn that RAISES yields `opaque()` — a sound skip,
        exactly as `node_contract.builtin_contract_for` degrades a raising built-in.

        Known interaction (safe, documented): a RAISING registered contract on a node that ALSO
        declares inline `provides:` loses that inline set — `resolve_contract` augments the
        non-None `opaque()` base, and `apply` ignores `provides` in opaque mode — whereas an
        UNREGISTERED node with the same inline `provides:` keeps it (`preserve_declared`). This
        only fires on a buggy plugin fn and can only MUTE checking, never manufacture a false
        positive, so `opaque()` (the resolver-wide safest answer) stays the right degrade."""
        if not isinstance(ntype, str):        # unhashable (e.g. `type: [..]`) would raise on .get
            return None
        fn = self._contracts.get(ntype)
        if fn is None:
            return None
        try:
            return fn(cfg if isinstance(cfg, dict) else {})
        except Exception:
            return opaque()

    def consumes_for(self, ntype: Any, cfg: Dict[str, Any],
                     base_path: Optional[str]) -> Optional["frozenset"]:
        """A CUSTOM type's registered consumes, or None for an unregistered / consumes-less type
        (→ the resolver falls to inline `consumes:` / empty). Never raises (mirrors
        `builtin_consumes_for`): a raising fn yields `frozenset()` (reads nothing checkable)."""
        if not isinstance(ntype, str):
            return None
        fn = self._consumes.get(ntype)
        if fn is None:
            return None
        try:
            return fn(cfg if isinstance(cfg, dict) else {}, base_path)
        except Exception:
            return frozenset()

    # --- D7.2: the composed sources the lint injects (0006-D7 §4 — composition in the caller) ---

    def contract_source(self) -> ContractFor:
        """A single injectable `contract_for(ntype, cfg)` chaining the two sources per 0006-D7 §4:
        BUILT-IN first (always authoritative — a manifest/registry never overrides a built-in),
        then this registry's CUSTOM slot. `register()` does NOT reject a builtin-named
        registration; it is this composition ORDER that keeps built-ins authoritative (a custom
        contract registered under `agent` is simply never consulted).

        Pass this to `analyze_dataflow(contract_for=...)`. NOTE: injecting the bare custom-only
        `contract_for` would be WRONG — it returns None for every built-in, collapsing them to
        opaque; the resolver's default is built-ins, so the injected source MUST re-include them,
        which this chain does."""
        def source(ntype: Any, cfg: Dict[str, Any]) -> Optional[Contract]:
            base = builtin_contract_for(ntype, cfg)
            return base if base is not None else self.contract_for(ntype, cfg)
        return source

    def consumes_source(self) -> ConsumesFor:
        """The consumes mirror of `contract_source`. Uses `is not None` (NOT `or`): a built-in
        reader can legitimately return an EMPTY frozenset (a render with `allow_unfilled`, an
        authoritative "reads nothing"), and `frozenset()` is falsy — an `or` would wrongly fall
        through to the registry and re-open the read-set the built-in closed."""
        def source(ntype: Any, cfg: Dict[str, Any],
                   base_path: Optional[str]) -> Optional["frozenset"]:
            base = builtin_consumes_for(ntype, cfg, base_path)
            return base if base is not None else self.consumes_for(ntype, cfg, base_path)
        return source
