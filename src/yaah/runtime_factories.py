"""Runtime factories — config-block → runtime-leaf builders for the bootstrap.

Used by: yaah.runtime (the assembly layer) — `_assemble_harness`/`run_root`/
`list_gates` call these to turn each root-config block (providers, prompt_sources,
data_*, mcp_*, transport, trace, state) into a constructed leaf or PrefixRouter.
Where: the config→runtime seam, split out of runtime.py so that file holds only
assembly + the CLI entrypoints. This is THE place the assembly layer reaches into
`adapters/` (the swap-in implementations).
Why: a new pluggable layer is a factory-map entry + a one-line `_build_router`
wrapper — not hand-rolled dispatch. Keeping the maps + helpers here keeps
runtime.py one concern (assemble + run).

Targets Python 3.9+.
"""
from __future__ import annotations

import inspect
import json
import os
from typing import Any, Dict, Optional

# Engine ports + zero-config references (next to the kernel) ...
from .agents import FakeBackend, RoutingBackend, ScriptedBackend
from .comms import InProcessComms
from .data import RoutingDataSink, RoutingDataSource
from .mcp import RoutingMcpSource, StaticMcpSource
from .prompts import RoutingPromptSource, StaticPromptSource
from .store import MemoryStore
from .trace import BusTracer, NullTracer
from .trace.contributors import BUILTIN_CONTRIBUTORS
# ... and the swap-in adapters (the only place the assembly layer reaches into adapters/).
from .adapters.backends import ClaudeCliBackend, LiteLLMBackend
from .adapters.backends.fake_tool_backend import FakeToolBackend
from .adapters.data import FileDataSource, FileSink, GitDiffSource
from .adapters.mcp import FileMcpSource
from .adapters.prompts import FilePromptSource, HttpPromptSource, LangfusePromptSource
from .adapters.stores import FileStore
from .adapters.trace import (
    ConsoleTraceSink,
    FileTraceSink,
    LangfuseTraceSink,
    ProgressFileSink,
    StatsFileSink,
)
from .adapters.transports import LocalBus, NatsComms


def _read_json(path: str) -> Any:
    """Load a JSON config, RESOLVING `_extends`.

    If the loaded object is a dict with a top-level `_extends: "base.json"` (path
    relative to this file's dir, or absolute), load the base recursively and
    DEEP-MERGE the current file on top:
      - child keys OVERRIDE base keys
      - child objects deep-merge into base objects
      - child LISTS REPLACE (lists in our configs are model lists / carry lists
        / tools — replace is what an overlay means)
      - child value `null` DELETES the key from the base (RFC 7396 — JSON Merge
        Patch). If you want a literal null in the merged result, put it in the
        canonical and inherit it (overlays can't introduce nulls — that's the
        tradeoff for the simple delete syntax).
    Cycles raise. The `_extends` field itself is dropped from the result.

    A base referenced as `_extends: "yaah:bases/local.base.json"` is a SEED
    shipped inside the package (resolved via importlib.resources, not the
    filesystem) — so the ref survives a `pip install` / public extraction where
    a `../yaah/configs/...` relative path would die. See `_resolve_pkg_ref`.

    Used by: the app's the app's fake overlay etc. — a thin overlay on its
    `_extends: the app's pipeline config` canonical sibling (the example app assessment
    #6b). The mechanism is also available to any future overlay use (a deployment
    variant, a per-tenant tweak); the canonical file stays one source of truth.
    """
    return _load_with_extends(path, _seen=())


_PKG_REF = "yaah:"  # _extends scheme for a seed shipped INSIDE the package


def _resolve_pkg_ref(ref: str) -> Any:
    """Load a packaged config seed referenced as `yaah:bases/local.base.json`
    (the 'non-path reference syntax' R14-seed needs to survive the public
    extraction). Resolves under the `yaah.configs` package via
    importlib.resources, so the SAME ref works from the source tree and from an
    installed wheel — no `../yaah/configs/...` relative path that dies on
    install. Returns the parsed JSON (a packaged seed must be self-contained:
    if it itself `_extends`, that ref must be another `yaah:` ref or absolute —
    a packaged resource has no filesystem dirname for a relative parent)."""
    import importlib.resources as ir

    rel = ref[len(_PKG_REF):].lstrip("/")
    trav = ir.files("yaah.configs")
    for part in rel.split("/"):
        trav = trav.joinpath(part)
    try:
        text = trav.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as e:
        raise ValueError(
            "_extends {!r}: no such packaged seed under yaah.configs "
            "(have you shipped configs/bases/*.json as package-data?)".format(ref)) from e
    try:
        cfg = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError("packaged seed {!r}: invalid JSON — {}".format(ref, e.msg)) from None
    if isinstance(cfg, dict) and "_extends" in cfg:
        parent = cfg.pop("_extends")
        if not (parent.startswith(_PKG_REF) or os.path.isabs(parent)):
            raise ValueError(
                "packaged seed {!r} _extends a relative path {!r}; a packaged "
                "seed must extend a 'yaah:' ref or an absolute path".format(ref, parent))
        base = (_resolve_pkg_ref(parent) if parent.startswith(_PKG_REF)
                else _load_with_extends(parent, _seen=()))
        return _deep_merge(base, cfg)
    return cfg


def _load_with_extends(path: str, *, _seen: tuple) -> Any:
    if path in _seen:
        raise ValueError("_extends cycle: {!r} via {}".format(
            path, " -> ".join(_seen + (path,))))
    with open(path, "r", encoding="utf-8") as f:
        try:
            cfg = json.load(f)
        except json.JSONDecodeError as e:
            # Name the file the decoder choked on. Without this the operator
            # gets a bare "Expecting value: line 1 column 1" with no hint which
            # of root / pipeline / decision / fixture file is malformed.
            raise ValueError("{}: invalid JSON — {}".format(path, e.msg)) from None
    if not isinstance(cfg, dict) or "_extends" not in cfg:
        return cfg
    base_path = cfg.pop("_extends")
    if base_path.startswith(_PKG_REF):  # a packaged seed — resolve via importlib.resources
        base = _resolve_pkg_ref(base_path)
    else:
        if not os.path.isabs(base_path):
            base_path = os.path.normpath(os.path.join(os.path.dirname(path), base_path))
        base = _load_with_extends(base_path, _seen=_seen + (path,))
    if not isinstance(base, dict):
        raise ValueError("_extends base {!r} is not a JSON object".format(base_path))
    return _deep_merge(base, cfg)


def _deep_merge(base: Any, overlay: Any) -> Any:
    """Deep-merge `overlay` over `base`. Returns a fresh value (does not mutate).
      - both dicts: per-key merge; `null` at a key DELETES it (JSON Merge Patch)
      - anything else: overlay value REPLACES base value (lists included)."""
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for k, v in overlay.items():
            if v is None:
                out.pop(k, None)  # JSON Merge Patch: null deletes
            elif k in base:
                out[k] = _deep_merge(base[k], v)
            else:
                out[k] = v
        return out
    return overlay


def _rel(base: str, path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(base, path)


def _load_price_map(pm: Any, base: str) -> Any:
    """`price_map` is an inline {model: {input, output}} dict OR a JSON file path
    (config-dir relative) — so an app keeps ONE rate card shared by every root
    instead of pasting the rates into each."""
    if isinstance(pm, str):
        pm_path = _rel(base, pm)
        with open(pm_path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError as e:
                raise ValueError("{}: invalid JSON — {}".format(pm_path, e.msg)) from None
    return pm


def _kw(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Spec keys except the dispatch 'type' — i.e. the leaf's constructor kwargs.
    Used by: the pass-through factories below (claude_cli/litellm/langfuse) that
    forward every remaining config key straight to the leaf constructor."""
    return {k: v for k, v in spec.items() if k != "type"}


def _scripted_by_model(spec: Dict[str, Any], base: str) -> Any:
    """The ScriptedBackend's canned-responses table: from a `fixtures` file
    (resolved against the config dir) or an inline `by_model`. Used by: the
    fake_scripted backend factory."""
    if "fixtures" in spec:
        return _read_json(_rel(base, spec["fixtures"]))
    return spec.get("by_model", {})


# Each map is {type-name: (factory(spec, base) -> leaf, spec-keys)}. The factory
# builds the leaf; spec-keys is the frozenset of config keys the factory reads
# besides 'type' — None means OPEN (the factory forwards every key to the leaf
# constructor, which raises TypeError on unknowns itself). validate.py (R15)
# reads these SAME maps for its type enums AND unknown-key checks, so adding a
# type = one entry here and the validator learns it for free — no parallel
# tables to drift (the sink/sinks bug class).
_BACKEND_TYPES = {
    "claude_cli": (lambda spec, base: ClaudeCliBackend(**_kw(spec)), None),
    "litellm": (lambda spec, base: LiteLLMBackend(**_kw(spec)), None),
    "fake": (lambda spec, base: FakeBackend(responses=spec.get("responses"),
                                            default=spec.get("default", "")),
             frozenset({"responses", "default"})),
    "fake_scripted": (lambda spec, base: ScriptedBackend(_scripted_by_model(spec, base),
                                                         default=spec.get("default", "")),
                      frozenset({"fixtures", "by_model", "default"})),
    # Scripted tool-loop backend — drives an `agent_loop` node from a list of
    # canned turn responses ({"text": "..."} or {"calls": [{name,args,id}, ...]}).
    # For tests + spike examples; proves the ApiProvider seam is replaceable.
    "fake_tool": (lambda spec, base: FakeToolBackend(turns=spec.get("turns", [])),
                  frozenset({"turns"})),
}
_PROMPT_TYPES = {
    "file": (lambda spec, base: FilePromptSource(_rel(base, spec.get("dir", "prompts")),
                                                 ext=spec.get("ext", ".md")),
             frozenset({"dir", "ext"})),
    "http": (lambda spec, base: HttpPromptSource(spec["base_url"]),
             frozenset({"base_url"})),
    "langfuse": (lambda spec, base: LangfusePromptSource(**_kw(spec)), None),
    "static": (lambda spec, base: StaticPromptSource(spec.get("prompts", {})),
               frozenset({"prompts"})),
}
_DATA_SOURCE_TYPES = {
    "git_diff": (lambda spec, base: GitDiffSource(
        repo=_rel(base, spec["repo"]) if spec.get("repo") else None,
        context=int(spec.get("context", 3)),
        intent_to_add=bool(spec.get("intent_to_add", False)),
        timeout=spec.get("timeout")),
        frozenset({"repo", "context", "intent_to_add", "timeout"})),
    "file": (lambda spec, base: FileDataSource(
        base_dir=_rel(base, spec.get("dir", "")) if spec.get("dir") else ""),
        frozenset({"dir"})),
}
_DATA_SINK_TYPES = {
    "file": (lambda spec, base: FileSink(
        base_dir=_rel(base, spec.get("dir", "")) if spec.get("dir") else ""),
        frozenset({"dir"})),
}
_MCP_TYPES = {
    "file": (lambda spec, base: FileMcpSource(_rel(base, spec.get("dir", ""))),
             frozenset({"dir"})),
    "static": (lambda spec, base: StaticMcpSource(spec.get("configs", {})),
               frozenset({"configs"})),
}


def _build_router(specs: Any, *, factories: Dict[str, Any], router_cls: Any,
                  default: Optional[str], base: str, optional: bool) -> Any:
    """Turn a {name: {type, ...}} config block into a PrefixRouter of leaves.

    Used by: the five _build_* wrappers below — the one place the "iterate specs,
    dispatch on `type`, wrap in a router" shape lives (it was copy-pasted per
    layer before). Where: the config->runtime seam. Why: a new pluggable layer is
    a factory map + a one-line wrapper, not another hand-rolled loop. `optional`
    distinguishes layers that are absent-OK (data/sink/mcp -> None) from layers
    that always exist (backend/prompt -> an empty router). `router_cls.label`
    gives the error message its layer-specific noun for free."""
    if not specs:
        return None if optional else router_cls({}, default=default)
    leaves: Dict[str, Any] = {}
    for name, spec in specs.items():
        entry = factories.get(spec.get("type"))
        if entry is None:
            raise ValueError("unknown {} type {!r}".format(router_cls.label, spec.get("type")))
        factory, _keys = entry
        leaves[name] = factory(spec, base)
    return router_cls(leaves, default=default)


def _build_backend(cfg: Dict[str, Any], base: str) -> RoutingBackend:
    return _build_router(cfg.get("providers"), factories=_BACKEND_TYPES,
                         router_cls=RoutingBackend, default=cfg.get("default_provider"),
                         base=base, optional=False)


def _build_prompt_source(cfg: Dict[str, Any], base: str) -> RoutingPromptSource:
    return _build_router(cfg.get("prompt_sources"), factories=_PROMPT_TYPES,
                         router_cls=RoutingPromptSource, default=cfg.get("default_prompt_source"),
                         base=base, optional=False)


def _build_data_source(cfg: Dict[str, Any], base: str) -> Optional[RoutingDataSource]:
    return _build_router(cfg.get("data_sources"), factories=_DATA_SOURCE_TYPES,
                         router_cls=RoutingDataSource, default=cfg.get("default_data_source"),
                         base=base, optional=True)


def _build_data_sink(cfg: Dict[str, Any], base: str) -> Optional[RoutingDataSink]:
    return _build_router(cfg.get("data_sinks"), factories=_DATA_SINK_TYPES,
                         router_cls=RoutingDataSink, default=cfg.get("default_data_sink"),
                         base=base, optional=True)


def _build_mcp_source(cfg: Dict[str, Any], base: str) -> Optional[RoutingMcpSource]:
    return _build_router(cfg.get("mcp_sources"), factories=_MCP_TYPES,
                         router_cls=RoutingMcpSource, default=cfg.get("default_mcp_source"),
                         base=base, optional=True)


# state-store backend extenders, keyed by type. Only the in-memory default ships;
# durable extenders (file / nats_kv / sqlite / ...) are added here per-deployment
# (see docs/durable-state.md) — none is baked in.
_STATE_TYPES = {
    "memory": (lambda spec, base: MemoryStore(), frozenset()),
    "file": (lambda spec, base: FileStore(_rel(base, spec.get("dir", "state"))),
             frozenset({"dir"})),
}


def _build_store(spec: Any, base: str) -> Any:
    """Build the durable-state Store from the root `state:` block (default memory).
    One store instance backs both the BatonStore and the IdempotencyStore (distinct
    key prefixes). Used by: run_root."""
    spec = spec or {"type": "memory"}
    t = spec.get("type", "memory")
    entry = _STATE_TYPES.get(t)
    if entry is None:
        raise ValueError("unknown state store type {!r}; have {}".format(t, sorted(_STATE_TYPES)))
    return entry[0](spec, base)


def _build_tls(spec: Any) -> Any:
    """Build an ssl.SSLContext from a {ca, cert, key, hostname} transport.tls block.
    `ca` verifies the server (remote-destination case); cert/key add a client
    certificate (mutual TLS) when the broker requires one."""
    if not spec:
        return None
    import ssl
    ctx = ssl.create_default_context(
        cafile=spec["ca"] if isinstance(spec, dict) and spec.get("ca") else None)
    if isinstance(spec, dict) and spec.get("cert") and spec.get("key"):
        ctx.load_cert_chain(certfile=spec["cert"], keyfile=spec["key"])
    return ctx


def _build_nats(cfg: Dict[str, Any], base: str) -> Any:
    """The nats transport factory — returns the (awaitable) connect() coroutine;
    _build_transport awaits it. Split out of the map because the tls/creds path
    resolution doesn't fit a lambda."""
    tls_spec = cfg.get("tls")
    if isinstance(tls_spec, dict):  # resolve cert paths relative to the config
        tls_spec = {k: (_rel(base, v) if k in ("ca", "cert", "key") and v else v)
                    for k, v in tls_spec.items()}
    return NatsComms(
        cfg.get("url", "nats://127.0.0.1:4222"),
        request_timeout=cfg.get("request_timeout", 300.0),  # LLM-node safe default (M1)
        user=cfg.get("user"), password=cfg.get("password"),
        token=cfg.get("token"),
        creds=_rel(base, cfg["creds"]) if cfg.get("creds") else None,
        tls=_build_tls(tls_spec),
        tls_hostname=(tls_spec or {}).get("hostname") if isinstance(tls_spec, dict) else None,
    ).connect()


# transport extenders — same (factory, spec-keys) shape as the maps above, so
# validate.py derives the transport enum + per-type keys from here (no hand-copy).
_TRANSPORT_TYPES = {
    "inproc": (lambda cfg, base: InProcessComms(), frozenset()),
    "localbus": (lambda cfg, base: LocalBus(), frozenset()),
    "nats": (_build_nats, frozenset({"url", "request_timeout", "user", "password",
                                     "token", "creds", "tls"})),
}


async def _build_transport(cfg: Dict[str, Any], base: str = "") -> Any:
    cfg = cfg or {}
    t = cfg.get("type", "inproc")
    entry = _TRANSPORT_TYPES.get(t)
    if entry is None:
        raise ValueError("unknown transport type {!r}; have {}".format(t, sorted(_TRANSPORT_TYPES)))
    out = entry[0](cfg, base)
    return (await out) if inspect.isawaitable(out) else out


# trace sink extenders, keyed by type. Add a destination = one entry here (same
# (factory, spec-keys) shape as the backend/prompt/state maps above) — no new
# dispatch code, and validate.py learns the type + its keys from this entry.
_TRACE_SINK_TYPES = {
    "file": (lambda spec, base: FileTraceSink(_rel(base, spec.get("path", "trace.jsonl"))),
             frozenset({"path"})),
    "console": (lambda spec, base: ConsoleTraceSink(), frozenset()),
    "langfuse": (lambda spec, base: LangfuseTraceSink(**_kw(spec)), None),  # host/keys -> client_opts
    # progress = live tailable stage lines; stats = the rolling aggregate snapshot.
    "progress_file": (lambda spec, base: ProgressFileSink(_rel(base, spec.get("path", "progress.log"))),
                      frozenset({"path"})),
    "stats_file": (lambda spec, base: StatsFileSink(_rel(base, spec.get("path", "stats.json")),
                                                    price_map=_load_price_map(spec.get("price_map"), base)),
                   frozenset({"path", "price_map"})),
}

# trace-block schema bits validate.py reads from HERE (single source, like the
# factory maps): the carriage modes _build_tracer dispatches on, and the keys
# the `trace:` block may carry.
_TRACE_MODES = ("none", "tracer", "envelope")
_TRACE_KEYS = frozenset({"mode", "capture", "sinks", "topic", "buffer_max"})


def _trace_sink_specs(spec: Dict[str, Any]) -> list:
    """Normalize `trace.sinks` (a single sink dict or a list) to a list. With none
    configured, default to the console sink so default-on tracing is VISIBLE out
    of the box (the basic progress UX); `sinks: []` opts out explicitly.

    The singular `sink` is REJECTED loudly: the factory used to read `sink` while
    the validator/defaults/seed-bases said `sinks` — a silent no-op that dropped
    every configured sink. validate's unknown-key check catches it first in
    normal flow; this raise is the last-line guard for direct callers."""
    if "sink" in spec:
        raise ValueError("trace.sink is not a key — use trace.sinks")
    s = spec.get("sinks")
    if s is None:
        return [{"type": "console"}]
    return s if isinstance(s, list) else [s]


async def _build_tracer(root: Dict[str, Any], comms: Any, base: str) -> Any:
    """Build the injected Tracer from the root `trace:` block and subscribe its
    sinks to the trace topic. Defaults: mode `tracer` (ON), capture `[phase]`,
    console sink — so a zero-config run gets live progress. `mode: none` =
    NullTracer (off). Two orthogonal axes: `mode` (carriage) and `capture` (which
    contributor modules fill the record). Used by: _assemble_harness.

    Async because a transport's subscribe may be async (NATS does network setup
    on subscribe; in-proc/LocalBus return synchronously) — we await it when it is,
    so the sink actually attaches over NATS (a dropped coroutine = no sink)."""
    spec = root.get("trace") or {}
    mode = spec.get("mode", "tracer")
    if mode == "none":
        return NullTracer()
    if mode not in _TRACE_MODES:
        raise ValueError("unknown trace.mode {!r}; have {}".format(mode, list(_TRACE_MODES)))
    contributors = []
    for name in spec.get("capture", ["phase"]):
        cls = BUILTIN_CONTRIBUTORS.get(name)
        if cls is None:
            raise ValueError("unknown trace capture {!r}; have {}".format(
                name, sorted(BUILTIN_CONTRIBUTORS)))
        contributors.append(cls())
    if mode == "envelope":
        # R6 carriage: no comms bus, no sinks — spans accrete in an in-memory per-corr
        # buffer the carrier process drains onto outgoing envelope.headers["trace"].
        # The orchestrator merges them back on receive (Harness ingests).
        from .trace import EnvelopeTracer
        return EnvelopeTracer(contributors=contributors,
                              buffer_max=int(spec.get("buffer_max", 256)))
    topic = spec.get("topic", "trace")
    for sspec in _trace_sink_specs(spec):
        entry = _TRACE_SINK_TYPES.get(sspec.get("type"))
        if entry is None:
            raise ValueError("unknown trace sink type {!r}; have {}".format(
                sspec.get("type"), sorted(_TRACE_SINK_TYPES)))
        await comms.subscribe(topic, entry[0](sspec, base).handle)  # Comms.subscribe is async (all transports)
    return BusTracer(comms, topic=topic, contributors=contributors)
