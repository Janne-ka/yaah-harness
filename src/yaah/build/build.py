"""build / serve_from_config / harness_from_config / build_graph (functions).

Used by: the runtime and apps to turn a pipeline config into a running setup.
Where: the top of the config-driven path.
Why:
  - build(): in-process — construct + register every node, return a Harness.
  - serve_from_config(): distributed worker side — build + serve_node each node.
  - harness_from_config(): orchestrator side — Graph + Harness over an existing Comms.
  - build_graph(): config → Graph.
Local spin-up = construct+register; cloud spin-up = serve/deploy (same config).

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# `backend` is typed as Any: the build layer only forwards the backend handle
# (a structural ApiProvider); it never introspects it, so a structural Any is
# the honest annotation.
from ..comms import Comms, InProcessComms
from ..harness import Graph, Harness, Stage
from ..runtime_factories import _read_json  # `_extends`-aware JSON loader
from ..validate import validate_pipeline  # re-exported for callers/tests
from .build_context import BuildContext
from .builders import _node_config, _wrap_node, default_registry
from .live_config_node import LiveConfigNode
from .live_leaf_config import LiveLeafConfig
from .macros import expand_macros
from .registry import Registry


def build_graph(g: Dict[str, Any]) -> Graph:
    stages: Dict[str, Stage] = {}
    for name, s in g["stages"].items():
        stages[name] = Stage(
            name=name,
            id=s.get("id"),  # configurable unique node id (gate addressing); defaults to name
            node=s.get("node", ""),  # a fork / fan-in stage carries no node
            validators=list(s.get("validators", [])),
            max_attempts=int(s.get("max_attempts", 1)),
            error_retries=int(s.get("error_retries", 2)),
            feedback=bool(s.get("feedback", False)),
            escalate=s.get("escalate"),
            then=s.get("then"),
            final=bool(s.get("final", False)),  # terminal-only: skip sticky re-fold on this output

            fanout=s.get("fanout"),  # role BARRIER: ask N workers, merge replies
            min_success=(int(s["min_success"]) if s.get("min_success") is not None
                         else None),  # k-of-n fanout completion (M9a)
            branch=s.get("branch"),
            fork=s.get("fork"),      # branch CHAINS: spread to N stages, fanin rejoins
            fanin=s.get("fanin"),
            foreach=s.get("foreach"),  # ADR-0007: bounded map of `node` over payload[items]
            wait=s.get("wait"),
            clears=[s["clears"]] if isinstance(s.get("clears"), str) else s.get("clears"),
            concerns_from=s.get("concerns_from"),  # payload key -> baton.concerns on pass
            concerns_into=s.get("concerns_into"),  # baton.concerns -> payload key pre-run
            effects_from=s.get("effects_from"),  # ADR-0008 D2: payload key -> completion-span `effects` (rollback handle)
            clearable=bool(s.get("clearable", True)),   # all nodes clearable by default
            on_error=s.get("on_error", "clear"),         # every node error-clears (default), override to compensate/None
        )
    return Graph(stages=stages, start=g["start"],
                 sticky=list(g.get("sticky", [])))  # fill-if-missing payload keys


def build(
    config: Dict[str, Any],
    *,
    comms: Optional[Comms] = None,
    backend: Optional[Any] = None,
    prompt_source: Optional[Any] = None,
    data_source: Optional[Any] = None,
    data_sink: Optional[Any] = None,
    mcp_source: Optional[Any] = None,
    idempotency_store: Optional[Any] = None,
    baton_store: Optional[Any] = None,
    envelope_store: Optional[Any] = None,
    tracer: Optional[Any] = None,
    registry: Optional[Registry] = None,
    base_dir: Optional[str] = None,
    run_dir: Optional[str] = None,
    live_config_path: Optional[str] = None,
    strict_resume: bool = True,
    lease_horizon: Optional[float] = None,
    lease_host: Optional[str] = None,
) -> Harness:
    # strict_resume MUST be forwarded to validate_pipeline: it tiers the
    # gate-route-not-in-form finding (ERROR when this harness will enforce the
    # declared form at resume, WARNING when lenient). A construction path that
    # builds with the root's strict_resume:false but validated with the default
    # True would hard-fail a route that is actually reachable at ITS runtime.
    validate_pipeline(config, base_path=base_dir, strict_resume=strict_resume)
    comms = comms or InProcessComms()
    registry = registry or default_registry()
    ctx = BuildContext(comms=comms, backend=backend, prompt_source=prompt_source,
                       data_source=data_source, data_sink=data_sink, mcp_source=mcp_source,
                       idempotency_store=idempotency_store, tracer=tracer, base_dir=base_dir,
                       run_dir=run_dir)
    register = getattr(comms, "register", None)
    if register is None:
        raise TypeError(
            "this Comms backend has no register(); local spin-up needs it "
            "(in cloud, nodes are deployed, not registered in-process)"
        )
    # live-vars mechanism (a): with a path, every node gets a per-call refresh
    # of its mutable leaves (model/knobs/numeric bounds) from the file — edit
    # the committed pipeline, the next invocation picks it up, no restart
    live = LiveLeafConfig(live_config_path) if live_config_path else None
    for role, node, node_cfg in _built_nodes(config, registry, ctx, live):
        register(role, node, node_cfg)
    # Both lease knobs are passed straight through: None means "unset" to the
    # Harness too, so the horizon number (DEFAULT_LEASE_HORIZON, normalized inside
    # LeaseState) and the host-name source (socket.gethostname()) stay written down
    # in exactly one place each.
    return Harness(comms, build_graph(config["graph"]),
                   baton_store=baton_store, envelope_store=envelope_store, tracer=tracer,
                   strict_resume=strict_resume,
                   lease_horizon=lease_horizon, lease_host=lease_host)


def _build_named(registry: Registry, spec: Dict[str, Any], ctx: BuildContext,
                 role: str) -> Any:
    """registry.build with the NODE ID on every failure. A builder error like
    "a 'shell' node needs 'command'" without the role name forced a human to
    diff overlay vs pipeline by hand (BUG-695 #6a) — the build knows exactly
    which node it was constructing, so say it."""
    try:
        return registry.build(spec, ctx)
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError("node {!r}: {}".format(role, e)) from e


def _built_nodes(config: Dict[str, Any], registry: Registry, ctx: BuildContext,
                 live: Optional[Any], roles: Optional[Any] = None):
    """Yield (role, wrapped_node, node_config) for each node — the construction that build()
    (in-process register) and serve_from_config() (serve over the bus) share; each caller then
    does its own sync/async delivery. `roles`, if given, restricts to a worker's subset."""
    for role, spec in config.get("nodes", {}).items():
        if roles is not None and role not in roles:
            continue
        spec = dict(spec)
        spec["_role"] = role
        # EXPAND THE PATH MACROS ONCE, HERE — this is the canonical build seam, and
        # everything downstream must see the SAME expanded spec. Before this line the
        # expansion happened only inside `registry.build`, which expands a COPY: the
        # builder got resolved paths but `_node_config(spec)` below still read the
        # ORIGINAL, so a `config: {"repo_root": "{run_dir}/repo"}` reached
        # `NodeConfig.extras` with the macro UNEXPANDED and a node reading its extras
        # got the literal string `{run_dir}/repo`. `_wrap_node` had the same blind
        # spot. Expanding here fixes both at once.
        #
        # `registry.build` KEEPS its own expansion deliberately: it is a public seam
        # an embedding app may call directly (`registry.build(spec, ctx)`), and that
        # path has no `_built_nodes` above it. Double expansion is a no-op — the
        # macro tokens are CONSUMED by the first pass, so the second finds nothing to
        # substitute (a surviving `{{key}}` is double-brace and never matches).
        spec = expand_macros(spec, ctx)
        node = _wrap_node(_build_named(registry, spec, ctx, role), spec, ctx)
        if live is not None:
            node = LiveConfigNode(node, role, live)
        yield role, node, _node_config(spec)


def harness_from_config(config: Dict[str, Any], comms: Comms,
                        *, baton_store: Optional[Any] = None,
                        envelope_store: Optional[Any] = None,
                        tracer: Optional[Any] = None,
                        strict_resume: bool = True,
                        lease_horizon: Optional[float] = None,
                        lease_host: Optional[str] = None) -> Harness:
    """Orchestrator side: build just the Graph + Harness over an existing Comms.
    Use with a distributed Comms whose nodes are served via serve_from_config()."""
    validate_pipeline(config, strict_resume=strict_resume)
    return Harness(comms, build_graph(config["graph"]),
                   baton_store=baton_store, envelope_store=envelope_store, tracer=tracer,
                   strict_resume=strict_resume,
                   lease_horizon=lease_horizon, lease_host=lease_host)


async def serve_from_config(
    config: Dict[str, Any],
    comms: Any,
    *,
    backend: Optional[Any] = None,
    prompt_source: Optional[Any] = None,
    data_source: Optional[Any] = None,
    data_sink: Optional[Any] = None,
    mcp_source: Optional[Any] = None,
    idempotency_store: Optional[Any] = None,
    tracer: Optional[Any] = None,
    registry: Optional[Registry] = None,
    roles: Optional[Any] = None,
    base_dir: Optional[str] = None,
    run_dir: Optional[str] = None,
    live_config_path: Optional[str] = None,
) -> list:
    """Worker side: build each node from config and serve it over the bus.
    `roles` optionally restricts which nodes this worker serves. Returns the
    list of served roles."""
    validate_pipeline(config, base_path=base_dir)
    registry = registry or default_registry()
    ctx = BuildContext(comms=comms, backend=backend, prompt_source=prompt_source,
                       data_source=data_source, data_sink=data_sink, mcp_source=mcp_source,
                       idempotency_store=idempotency_store, tracer=tracer, base_dir=base_dir,
                       run_dir=run_dir)
    serve = getattr(comms, "serve_node", None)
    if serve is None:
        raise TypeError("this Comms has no serve_node(); use build() for in-process register()")
    live = LiveLeafConfig(live_config_path) if live_config_path else None
    served = []
    for role, node, node_cfg in _built_nodes(config, registry, ctx, live, roles=roles):
        await serve(role, node, node_cfg)
        served.append(role)
    return served


def build_from_json(path: str, **kwargs: Any) -> Harness:
    return build(_read_json(path), **kwargs)
