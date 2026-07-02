"""plugins — the public registration seam for config-selectable extension types.

Used by: extension modules named in a root config's `plugins: ["my_ext", ...]`
list (imported by the CLI / run_root BEFORE validation), or called directly by
an embedding app at startup.
Where: the one public door into the factory maps in `runtime_factories` — the
same maps `validate.py` derives its type enums and per-type key checks from, so
a registered type validates and builds with no further wiring. (Editor
autocomplete is builtin-only: the committed schemas are generated without
plugins; an embedding app can call schema_gen.build_root_schema AFTER
registering to get a plugin-aware schema.)
Why: without this, adding a RedisBackend or an OllamaProvider meant FORKING the
engine (the maps were private) while `validate` actively rejected the unknown
type string. Registration makes "wiring is data" true for third parties: ship a
module that calls register_type at import time, name it in `plugins:`, done.

Example extension module (importable from the config's dir, like fn: targets):

    # my_ext.py
    from yaah.plugins import register_type
    register_type("state", "redis",
                  lambda spec, base: RedisBackend(spec.get("url", "redis://localhost")),
                  spec_keys=["url"])

Node TYPES are the one kind not covered here: they already have a public
programmatic path (`yaah.build.Registry.register` + `build(registry=...)`).

Trust boundary — READ THIS: `plugins:` imports and RUNS the named modules, and
it does so EARLIER than `fn:` targets — at validate/explain time, not just at
run time (`yaah validate untrusted.json` executes its plugins; the validator
can't know a plugin's types without importing it). Only validate configs whose
plugins you trust; every CLI action prints which plugins it imported.

Flat-name caveat (same as fn: targets, see cli.py): in a long-lived process
that loads MANY configs, two configs each shipping a flat `my_ext.py` collide —
Python caches imports by name, so the second config silently gets the first's
module and its own registrations never run. The durable fix is a packaged
dotted name (`plugins: ["mypkg.yaah_ext"]`), not a flat file.

Targets Python 3.9+.
"""
from __future__ import annotations

import importlib
from typing import Any, Callable, Iterable, Optional

Factory = Callable[[Any, str], Any]  # (spec, base_dir) -> built instance

# kind (the config vocabulary) -> the factory-map attribute in runtime_factories.
# Every map has the same {name: (factory, spec_keys|None)} shape; validate.py and
# schema_gen read the SAME maps, which is why one register call is enough.
_KINDS = {
    "provider":      "_PROVIDER_TYPES",       # root `providers:` entries
    "prompt_source": "_PROMPT_TYPES",         # root `prompt_sources:`
    "data_source":   "_DATA_SOURCE_TYPES",    # root `data_sources:`
    "data_sink":     "_DATA_SINK_TYPES",      # root `data_sinks:`
    "mcp_source":    "_MCP_TYPES",            # root `mcp_sources:`
    "state":         "_STATE_TYPES",          # root `state:` block
    "transport":     "_TRANSPORT_TYPES",      # root `transport:` block
    "trace_sink":    "_TRACE_SINK_TYPES",     # root `trace.sinks[]` entries
}


def register_type(kind: str, name: str, factory: Factory, *,
                  spec_keys: Optional[Iterable[str]] = None) -> None:
    """Register a new config-selectable `type` for `kind`.

    `factory(spec, base_dir)` builds the instance from its config entry.
    `spec_keys` are the entry keys the factory reads (unknown keys are then
    rejected at validation, same as built-ins); None = open spec (the
    constructor enforces its own kwargs, like claude_cli).

    Fails loud on an unknown kind (with the valid list), a non-str name, or a
    name collision (a plugin silently REPLACING the built-in `file` store would
    be the worst kind of surprise)."""
    if kind not in _KINDS:
        raise ValueError("unknown plugin kind {!r}; valid kinds: {}".format(
            kind, ", ".join(sorted(_KINDS))))
    if not (isinstance(name, str) and name):
        raise ValueError("type name must be a non-empty string, got {!r}".format(name))
    if not callable(factory):
        raise ValueError("factory for {}:{} must be callable, got {!r}".format(
            kind, name, factory))
    from . import runtime_factories as rf
    type_map = getattr(rf, _KINDS[kind])
    if name in type_map:
        raise ValueError(
            "{} type {!r} is already registered — a plugin must not silently "
            "replace an existing type; pick another name".format(kind, name))
    keys = frozenset(spec_keys) if spec_keys is not None else None
    type_map[name] = (factory, keys)


def load_plugins(modules: Any, base: str) -> None:
    """Import each module named in a root config's `plugins:` list, so its
    import-time `register_type` calls run. Must happen BEFORE validate_root
    (the validator learns registered types from the same maps).

    Modules resolve like `fn:` targets — the config's dir is on sys.path, so a
    `my_ext.py` next to the config imports as "my_ext"; shared extensions are
    normal installed packages. A re-load of the SAME module is idempotent
    (Python caches the import; register calls don't re-run) — but see the
    module docstring's flat-name caveat: a DIFFERENT config's same-named flat
    module is served from that cache too. Import/registration errors surface
    loudly with the module named."""
    if not modules:
        return
    if not (isinstance(modules, list)
            and all(isinstance(m, str) and m for m in modules)):
        raise ValueError('`plugins` must be a list of module-path strings, '
                         'got {!r}'.format(modules))
    import sys
    if base and base not in sys.path:
        sys.path.insert(0, base)   # same convention as fn: targets (cli does this too)
    for mod in modules:
        try:
            importlib.import_module(mod)
        except Exception as e:
            raise ValueError("plugins: failed to load {!r}: {}: {}".format(
                mod, type(e).__name__, e)) from e
