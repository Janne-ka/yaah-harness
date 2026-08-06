"""templating — the one {{mustache}} placeholder regex + fill, shared by everything that
performs or reasons about template substitution: the render node, the human-gate prompt, and
the data-flow lint (which extracts the {{keys}} a render reads). ONE copy so the lint and the
runtime can never disagree on which tokens a template fills. Dependency-free (only `re`), cheap
to import.

Example:
    fill("hi {{name}}, {{missing}}", {"name": "Sam"})  ->  ("hi Sam, {{missing}}", ["missing"])

Targets Python 3.9+.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

# DELIBERATE NON-FEATURE: this render/gate templater does NOT recognize the
# agent-prompt dialect's `?`/`!` sigils (agents/agent.py::_PLACEHOLDER — `{{?key}}`
# optional, `{{!key}}` untrusted-fenced). Those sigils are not `\w`, so a `{{?key}}`
# or `{{!key}}` in a render template does not match here and passes through as a
# LITERAL. Per-key optionality and per-key fencing in render templates are a
# deliberate non-feature until an author asks (a render output feeds a human/file,
# not a model prompt, so fencing has no consumer here; whole-node optionality is
# `allow_unfilled`). See .notes/deferred-ledger-2026-07-07.md §A.
PLACEHOLDER = re.compile(r"{{\s*(\w+)\s*}}")

# --- the OTHER brace dialect: SINGLE-brace build-time path macros --------------
# Spelled here because two layers must agree on them: `yaah.build.macros` EXPANDS
# them when a node is built, and this module's lint seam has to recognize one of
# them in a `template_file` path. This module is the leaf both can import —
# importing `yaah.build` the other way round pulls the whole builder stack into
# `yaah validate`, which must stay cheap.
BASE_DIR_MACRO = "{base_dir}"
RUN_DIR_MACRO = "{run_dir}"


def macro_matcher(token: str) -> "re.Pattern":
    """A matcher for a single-brace macro `token` that does NOT fire inside a
    DOUBLE-brace `{{token}}` run-time placeholder. The two dialects collide by name
    — `{{run_dir}}` contains `{run_dir}` as a substring — so a plain `str.replace`
    rewrites a run-time placeholder to `{<abs path>}` at build time and the fill it
    was waiting for never happens.

    The guard is one brace on each side, which is the whole grammar either dialect
    has; two accepted consequences of that:
      - LOPSIDED braces do not expand. `"{{run_dir}"` and `"{run_dir}}"` are neither
        a macro nor a valid placeholder, and they pass through UNCHANGED and silent —
        no expansion, no error. Guessing which dialect a malformed string meant is
        worse than leaving it alone.
      - `"{{{run_dir}}}"` expands the OUTER pair's worth: the inner `{run_dir}` is
        brace-wrapped so it is skipped at build, leaving `{{run_dir}}` for the
        run-time fill inside a literal brace pair. That is the composition the two
        dialects imply, not a special case."""
    return re.compile(r"(?<!\{)" + re.escape(token) + r"(?!\})")


_BASE_DIR_IN_PATH = macro_matcher(BASE_DIR_MACRO)
_RUN_DIR_IN_PATH = macro_matcher(RUN_DIR_MACRO)


def unresolvable_macro(rnode: Dict[str, Any]) -> Optional[str]:
    """The build-time macro in this node's `template_file` that CANNOT be resolved
    where templates are read statically, or None when nothing blocks the read. Only
    `{run_dir}` qualifies: it names the run's artifact root, which does not exist yet.

    Exists so a caller can tell "this template reads nothing" from "this template
    could not be read" — `render_template_text` answers None for both, and treating
    the second as the first turns an unchecked node into a clean bill."""
    tfile = rnode.get("template_file")
    if isinstance(tfile, str) and _RUN_DIR_IN_PATH.search(tfile):
        return RUN_DIR_MACRO
    return None


def render_template_text(rnode: Dict[str, Any], base_path: Optional[str]) -> Optional[str]:
    """A render node's template SOURCE, or None when it can't be read statically (skip — not
    the linter's job to report a missing file). Inline `template_text` is always available; a
    `template_file` is read relative to `base_path` (the root config's dir, matching
    `_build_render`) when known, else by absolute path. Never raises.

    A `template_file` may carry the build-time path macros, and the two resolve
    DIFFERENTLY here: `{base_dir}` IS `base_path` at this seam, so it is expanded and
    the file is read like any other; `{run_dir}` names an artifact root that does not
    exist until the run starts, so the file cannot be read at lint time. Callers that
    must not degrade silently on the second case ask `unresolvable_macro(rnode)`
    first."""
    inline = rnode.get("template_text")
    if isinstance(inline, str):
        return inline
    tfile = rnode.get("template_file")
    if not isinstance(tfile, str) or not tfile:
        return None
    expanded = (_BASE_DIR_IN_PATH.sub(lambda _m: os.path.abspath(base_path), tfile)
                if base_path else tfile)
    if expanded != tfile:
        # ALREADY ROOTED at the base — the macro expanded to the ABSOLUTE base dir
        # (what `build.macros` substitutes). It must NOT also be joined against
        # `base_path` below: with a RELATIVE base_path that join prefixes the base a
        # second time ("rel/dir/rel/dir/x"), the open fails, and the caller reads the
        # None as "this template has nothing to check".
        path = os.path.abspath(expanded)
    elif os.path.isabs(tfile):
        path = tfile
    elif base_path:
        path = os.path.join(base_path, tfile)
    else:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def fill(template: str, payload: dict) -> Tuple[str, List[str]]:
    """Substitute {{key}} from the payload; return (filled_text, names_with_NO_value). A
    missing key renders the LITERAL `{{key}}` unchanged — that once silently shipped a broken
    report/spec at exit 0 (the worst fault class), so the caller surfaces the unfilled set to
    make it observable. Values are stringified; the template is trusted config, not payload."""
    unfilled: List[str] = []

    def sub(m: "re.Match") -> str:
        k = m.group(1)
        if k not in payload:
            if k not in unfilled:
                unfilled.append(k)
            return m.group(0)
        v = payload[k]
        return v if isinstance(v, str) else str(v)

    return PLACEHOLDER.sub(sub, template), unfilled
