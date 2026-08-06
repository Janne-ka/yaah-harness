"""expand_macros — BUILD-TIME path macros in a node spec (`{base_dir}`, `{run_dir}`).

Used by: `build._built_nodes` — the CANONICAL seam, shared by in-process `build()`
and worker-side `serve_from_config()`. It expands before the spec is handed to the
builder AND before `_wrap_node` / `_node_config` read it, so a macro in a node's
`config` extras reaches `NodeConfig.extras` resolved. `Registry.build` expands too,
covering a direct `registry.build(spec, ctx)` by an embedding app.
Where: yaah.build, between the config spec and the builder that reads it.

IDEMPOTENT by construction, which is what makes those two call sites safe to
stack: expansion CONSUMES the `{base_dir}` / `{run_dir}` tokens, so a second pass
over an already-expanded spec finds nothing to substitute. (It is not idempotent in
the pathological sense of a base_dir that itself contains a macro token — a path
literally named `{run_dir}` — which is not a case worth defending.)
Why: a config file must stay RELOCATABLE (a repo-relative path a colleague can
check out anywhere) while the runtime needs an ABSOLUTE path — a repo-bound agent
runs with cwd in the task worktree, and a shell node's `cwd` is wherever the
author set it. The macro is resolved once, at build, against the config's own
base directory or the run's artifact root.

  `{base_dir}`  -> the directory the root config was loaded from (absolute).
  `{run_dir}`   -> the run's artifact root (root key `run_dir`, absolute).

This LIFTED the `{base_dir}` expansion that used to be a closure inside
`_build_agent`, which is why `{base_dir}` now works on EVERY node type rather than
only on an agent's tool strings — intentional: the reason it existed (relocatable
file, absolute runtime path) was never agent-specific, and having two
implementations would guarantee they drift.

NO ENGINE DEFAULT for `run_dir`. A missing root key is a build ERROR naming it,
never a quiet fall back to cwd or to `base_dir`: writing a run's artifacts into
the library tree because a host fact was forgotten is the silent-misroute class
this codebase fails loud on.

NOT the same thing as `{{key}}` interpolation. These macros are SINGLE-brace and
expand at BUILD time; `{{db_url}}` is double-brace and is filled per invocation
from the envelope at RUN time (yaah.templating). One string may carry both, and a
`{{run_dir}}`/`{{base_dir}}` placeholder is the case where they COLLIDE by name:
the macro token is a substring of the placeholder, so a plain `str.replace` would
rewrite `{{run_dir}}` to `{<abs path>}` at build and the run-time fill would never
happen. The matcher below is brace-anchored (`_MATCHERS`) precisely so a token
wrapped in a second pair of braces is left alone.

Targets Python 3.9+.
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable, Dict, Optional

from ..templating import (BASE_DIR_MACRO as BASE_DIR, RUN_DIR_MACRO as RUN_DIR,
                          macro_matcher)
from .build_context import BuildContext


def _base_dir(ctx: BuildContext) -> str:
    if not ctx.base_dir:
        raise ValueError(
            "node config uses {base_dir} but no base_dir was passed to build()")
    return os.path.abspath(ctx.base_dir)


def _run_dir(ctx: BuildContext) -> str:
    if not ctx.run_dir:
        raise ValueError(
            "node config uses {run_dir} but the root config has no `run_dir` key — "
            "set it to this run's artifact root (base-relative or absolute); there "
            "is deliberately no default, so artifacts can never land somewhere "
            "nobody chose")
    return os.path.abspath(ctx.run_dir)


# THE macro table: token -> what it resolves to for a given build. A third macro
# is one entry here — the matcher and the walk derive from it.
_MACROS: Dict[str, Callable[[BuildContext], str]] = {
    BASE_DIR: _base_dir,
    RUN_DIR: _run_dir,
}

# One compiled matcher per token, brace-anchored: a token WRAPPED in a second pair
# of braces (`{{run_dir}}`) is a run-time placeholder for another layer to fill and
# must survive the build untouched. The anchoring lives in `yaah.templating` — the
# module that owns both brace dialects — so the lint seam that has to recognize the
# same tokens in a path cannot anchor them differently.
_MATCHERS: Dict[str, "re.Pattern"] = {token: macro_matcher(token) for token in _MACROS}


def _expand_str(s: str, ctx: BuildContext) -> str:
    if "{" not in s:              # the overwhelmingly common case — no scan, no copy
        return s
    for token, resolve in _MACROS.items():
        matcher = _MATCHERS[token]
        if matcher.search(s) is None:
            continue              # absent, or present only as `{{token}}` — nothing to do
        # The replacement is a callable, not a template string: a resolved path is
        # a literal, and `sub`'s template dialect would eat a backslash in it.
        value = resolve(ctx)
        s = matcher.sub(lambda _m, v=value: v, s)
    return s


def expand_macros(spec: Any, ctx: BuildContext) -> Any:
    """Return `spec` with every STRING LEAF macro-expanded, rebuilding the
    containers so the caller's config object is never mutated (a spec is read
    again by `_node_config`, by the lint, and by a live re-read). Walks dicts and
    lists to any depth, which is what reaches a shell node's `command` elements, an
    agent's `tools[].usage`, a render's `out`/`template_file`, a get/post `source`/
    `sink`, and a node's `config` extras — without this module having to know which
    key each node type keeps its paths under."""
    if isinstance(spec, str):
        return _expand_str(spec, ctx)
    if isinstance(spec, dict):
        return {k: expand_macros(v, ctx) for k, v in spec.items()}
    if isinstance(spec, list):
        return [expand_macros(v, ctx) for v in spec]
    return spec


def resolve_run_dir(run_dir: Optional[str], base: str,
                    create: bool = True) -> Optional[str]:
    """Absolutize the root's `run_dir` against the config's base dir and (by default)
    CREATE it. Called from the runtime's assembly, where the base is known; returns
    None when the key is absent (the macro then refuses at build, by design).

    `create=False` for a TEARDOWN verb (`yaah clear`): it assembles a harness only to
    reach the clear/flush primitives, and a verb whose whole job is to remove state
    must not leave a freshly-made directory behind — least of all on a root whose run
    never happened. The path is still resolved, so `{run_dir}` in a node spec builds
    exactly as it would for a run."""
    if not run_dir:
        return None
    path = run_dir if os.path.isabs(run_dir) else os.path.join(base, run_dir)
    path = os.path.abspath(path)
    if create:
        os.makedirs(path, exist_ok=True)
    return path
