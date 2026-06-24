"""Scaffold-template loader for `yaah init` and `yaah scaffold`.

A fresh user runs `pip install yaah-harness && yaah init my-pipeline` and gets
a runnable `agent -> validate -> parse -> render` pipeline on fake providers —
no repo checkout required. The `yaah scaffold <archetype> <dir>` form lets
them pick which archetype to start from; `init` defaults to `linear` (the
hello-yaah shape) for back-compat and quick-start ergonomics.

Templates live as ACTUAL FILES under `src/yaah/templates/<archetype>/`. Each
archetype dir holds the file tree the scaffold writes (`starter.json`,
`prompts/foo.md`, etc.). Loading reads them via `importlib.resources`, the
same mechanism `_resolve_pkg_ref` uses for `yaah:bases/...` configs — works
from the source tree and from an installed wheel without `__file__` games.

Earlier this module embedded each template's contents as Python string
literals (~340 lines of escaped JSON/HTML/Markdown). The files-as-files
shape lets editors syntax-check the JSON/HTML/MD natively, eliminates the
escape-the-quote-in-the-quote pain, and matches the package-data pattern
already used for `yaah.configs.bases`.

Called by `yaah.cli._dispatch` when `spec["action"] == "scaffold"` (init is
an alias). See `docs/archetypes.md` for what each archetype is for.

Targets Python 3.9+.
"""
from __future__ import annotations

import importlib.resources as ir
import os
from typing import Dict, List


# The archetypes the engine ships. Each name must have a matching directory
# under `yaah.templates/<name>/`. See `docs/archetypes.md`.
ARCHETYPES: List[str] = ["linear", "branch-with-gate", "fork-fanin"]


# One-line descriptions surfaced by `yaah scaffold --list`. Kept here next to
# ARCHETYPES so a new entry is visibly incomplete until both get the new key
# (the test in test_init_template asserts the two stay in sync).
ARCHETYPE_DESCRIPTIONS: Dict[str, str] = {
    "linear":           "one stage after another (agent → render). Use for: smoke tests, demos, single-shot transforms.",
    "branch-with-gate": "a stage that branches on a verdict, with a human-decision gate parking the run mid-way. Use for: review/approve flows.",
    "fork-fanin":       "fan out to N parallel branches, fan in to one reducer. Use for: candidates × verdict, scout/prefetch/act.",
}


def _walk(node: "ir.abc.Traversable", rel: str, out: Dict[str, str]) -> None:
    """Recurse the packaged template dir, collecting (relpath -> content)
    pairs. Skips Python build artifacts the user never asked for: `__init__.py`
    (a package marker so `importlib.resources.files()` resolves the dir),
    `__pycache__/` (bytecode cache created on first import — `.pyc` files
    aren't UTF-8 and shouldn't land in a scaffolded user dir), and `.pyc`
    leftovers anywhere."""
    for child in node.iterdir():
        name = child.name
        if name == "__init__.py" or name == "__pycache__" or name.endswith(".pyc"):
            continue
        child_rel = "{}/{}".format(rel, name) if rel else name
        if child.is_dir():
            _walk(child, child_rel, out)
        else:
            out[child_rel] = child.read_text(encoding="utf-8")


def load_template(archetype: str) -> Dict[str, str]:
    """Return `{relpath: content}` for the named archetype, loaded from
    packaged data. Raises ValueError if the archetype isn't shipped."""
    if archetype not in ARCHETYPES:
        raise ValueError(
            "unknown archetype {!r}; known: {} — see `docs/archetypes.md` "
            "for what each shape is for".format(archetype, sorted(ARCHETYPES)))
    root = ir.files("yaah.templates").joinpath(archetype)
    out: Dict[str, str] = {}
    _walk(root, "", out)
    return out


def scaffold(target_dir: str, archetype: str = "linear") -> int:
    """Write the named archetype's template into `target_dir`. Returns file
    count.

    Refuses to overwrite if `target_dir` already exists and is non-empty —
    `yaah init` / `yaah scaffold` must never silently clobber a user's work.

    Raises ValueError if the archetype is unknown — the message lists the
    known names so the operator (or agent) can self-correct."""
    if archetype not in ARCHETYPES:
        raise ValueError(
            "unknown archetype {!r}; known: {} — see `docs/archetypes.md` "
            "for what each shape is for".format(archetype, sorted(ARCHETYPES)))
    if os.path.exists(target_dir) and os.listdir(target_dir):
        raise FileExistsError(
            "{!r} exists and is not empty — refusing to overwrite".format(target_dir))
    template = load_template(archetype)
    for relpath, content in template.items():
        path = os.path.join(target_dir, relpath)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
    return len(template)
