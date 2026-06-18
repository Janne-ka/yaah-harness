"""Transforms for the arch-drift example pipeline.

Each function is a `transform` node with `call: "envelope"` — signature
`fn(envelope, config) -> dict` whose returned dict SPREADS over the payload
top-level (so downstream {{key}} placeholders resolve).

Snapshot strategies are intentionally pluggable: the pipeline references one by
name (e.g. `fn:transforms:snapshot_imports`), so swapping is a JSON edit, not a
code change. Adding a new strategy = one function here + one config edit.

Run-shape: snapshot -> read_committed_svg -> agent -> parse_extracted ->
render_mermaid -> diff_svgs -> branch -> render report -> human_gate ->
write_versioned (on approve).

Targets Python 3.9+.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List


# ---------- snapshot strategies (pluggable) ----------------------------------

def snapshot_imports(envelope, config) -> Dict[str, Any]:
    """Default snapshot: walk `repo_path` and emit a compact text picture of
    top-level packages + their module docstrings + the imports each module
    pulls in. Bounded by file count and per-file line cap so large repos still
    fit in a prompt budget. Seeds `feedback: ""` so the prompt's
    `{{feedback}}` placeholder resolves on the first pass."""
    repo = os.path.abspath(envelope.payload.get("repo_path", "."))
    src = os.path.join(repo, "src") if os.path.isdir(os.path.join(repo, "src")) else repo
    lines: List[str] = ["# Repo snapshot for architecture extraction",
                        "# root: {}".format(os.path.basename(repo)),
                        "# strategy: snapshot_imports", ""]
    pkgs = sorted(_top_level_packages(src))
    for pkg in pkgs[:20]:                              # cap: 20 packages
        lines.append("## package: {}".format(pkg))
        pkg_dir = os.path.join(src, pkg)
        init = os.path.join(pkg_dir, "__init__.py")
        if os.path.isfile(init):
            doc = _module_docstring(init)
            if doc:
                lines.append(doc.strip())
        lines.append("")
        for mod in sorted(_python_files(pkg_dir))[:15]:  # cap: 15 modules/pkg
            rel = os.path.relpath(mod, src)
            lines.append("- module: {}".format(rel))
            doc = _module_docstring(mod)
            if doc:
                lines.append("  doc: {}".format(_first_sentence(doc)))
            imports = _internal_imports(mod, pkgs)
            if imports:
                lines.append("  imports: {}".format(", ".join(sorted(imports))))
        lines.append("")
    snapshot = "\n".join(lines)
    # transform `call: "envelope"` REPLACES the payload with what we return
    # (transform_node.py:88), so we must carry forward the keys later stages
    # need — here, the input config (repo_path / arch_svg_*) used by write_versioned.
    return {**envelope.payload, "snapshot": snapshot, "feedback": ""}


# Future strategies (one-function adds): snapshot_readme_first (read
# docs/README + top of architecture.md), snapshot_changed_files_only (git diff
# against a baseline), snapshot_top_modules_only (no imports, names only).


# ---------- read the currently-committed SVG ---------------------------------

def read_committed_svg(envelope, config) -> Dict[str, Any]:
    """Read the currently-committed architecture SVG, returning empty string if
    the file does not exist (first-run case: drift is trivially 'yes' and the
    pipeline lands the initial version)."""
    path = envelope.payload.get("arch_svg_path", "docs/architecture.svg")
    abs_path = os.path.join(os.path.abspath(envelope.payload.get("repo_path", ".")), path)
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            return {**envelope.payload, "committed_svg": f.read(),
                    "committed_svg_path": abs_path}
    except FileNotFoundError:
        return {**envelope.payload, "committed_svg": "",
                "committed_svg_path": abs_path}


# ---------- parse the agent's JSON reply -------------------------------------

def parse_extracted(envelope, config) -> Dict[str, Any]:
    """Parse the agent's raw reply as JSON, spreading {mermaid, notes} onto the
    payload. The validator (`json_object`, required: ['mermaid']) has already
    confirmed the keys are present; this lifts them into real payload slots so
    `render_mermaid` and the report template can read them as `{{mermaid}}` /
    `{{notes}}`."""
    raw = envelope.payload.get("raw", "{}")
    obj = json.loads(raw)
    return {**envelope.payload, "mermaid": obj.get("mermaid", ""),
            "notes": obj.get("notes", "")}


# ---------- render mermaid -> SVG (mmdc shell-out, with `:canned` fake) ------

# A canned SVG for offline / fake runs. Matches the rough shape mermaid-cli
# would produce so the diff stage produces something realistic, not an
# artificial 'identical' result. The fake provider feeds a specific mermaid
# string in `arch-drift.local.json`; this SVG matches that pre-baked output.
_CANNED_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="120">'
    '<rect x="10" y="10" width="80" height="40" fill="#eef"/>'
    '<rect x="120" y="10" width="80" height="40" fill="#eef"/>'
    '<rect x="230" y="10" width="80" height="40" fill="#eef"/>'
    '<text x="50" y="35" text-anchor="middle">core</text>'
    '<text x="160" y="35" text-anchor="middle">harness</text>'
    '<text x="270" y="35" text-anchor="middle">adapters</text>'
    '</svg>'
)


def render_mermaid(envelope, config) -> Dict[str, Any]:
    """Render `mermaid` to SVG. Shells out to `mmdc` (mermaid-cli) by default;
    when `MERMAID_RENDERER=:canned` is in the environment, returns a pre-baked
    SVG so the example runs offline without npm/mmdc installed (used by the
    `.local.json` overlay). Raises with an actionable message if `mmdc` is
    declared but not on PATH."""
    renderer = os.environ.get("MERMAID_RENDERER", "mmdc")
    if renderer == ":canned":
        return {**envelope.payload, "new_svg": _CANNED_SVG}
    if shutil.which(renderer) is None:
        raise RuntimeError(
            "mermaid renderer {!r} not on PATH — install with "
            "`npm install -g @mermaid-js/mermaid-cli`, or set "
            "`MERMAID_RENDERER=:canned` for an offline run".format(renderer))
    mermaid = envelope.payload.get("mermaid", "")
    with tempfile.TemporaryDirectory() as tmp:
        mmd, svg = os.path.join(tmp, "g.mmd"), os.path.join(tmp, "g.svg")
        with open(mmd, "w", encoding="utf-8") as f:
            f.write(mermaid)
        # -q for quieter output; mmdc still prints progress on stderr
        result = subprocess.run([renderer, "-i", mmd, "-o", svg, "-q"],
                                capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError("mmdc failed (exit {}): {}".format(
                result.returncode, result.stderr.strip()[:500]))
        with open(svg, "r", encoding="utf-8") as f:
            return {**envelope.payload, "new_svg": f.read()}


# ---------- diff the two SVGs ------------------------------------------------

_SVG_ID = re.compile(r'\bid="[^"]+"')
_WHITESPACE = re.compile(r"\s+")


def _normalize_svg(svg: str) -> str:
    """Strip ids (mermaid generates run-unique ones) and collapse whitespace, so
    'logically identical' SVGs compare equal regardless of cosmetic noise."""
    return _WHITESPACE.sub(" ", _SVG_ID.sub("", svg)).strip()


def diff_svgs(envelope, config) -> Dict[str, Any]:
    """Compare committed vs newly-rendered SVG. Output `changed: 'yes'|'no'`
    (strings, not bool, because yaah's branch matches on string repr) and a
    one-line `summary`. First-run case (`committed_svg == ""`) is always
    changed=yes — the pipeline lands the initial version."""
    committed = envelope.payload.get("committed_svg", "")
    new = envelope.payload.get("new_svg", "")
    if not committed:
        return {**envelope.payload, "changed": "yes",
                "summary": "no committed SVG at {}; this run will land the initial version".format(
                    envelope.payload.get("committed_svg_path", "<unknown>"))}
    if _normalize_svg(committed) == _normalize_svg(new):
        return {**envelope.payload, "changed": "no",
                "summary": "committed SVG matches the architecture extracted from the code"}
    return {**envelope.payload, "changed": "yes",
            "summary": "architecture extracted from the code differs from the committed SVG"}


# ---------- versioned write (on approve) -------------------------------------

def write_versioned(envelope, config) -> Dict[str, Any]:
    """Write the approved SVG to a versioned path under `arch_svg_dir`
    (default: `docs/architecture/`), update `latest.svg` to point at it, and
    return provenance. Does NOT git-commit — auto-committing to the user's
    repo crosses a safety line. The pipeline prints the suggested next step
    instead."""
    repo = os.path.abspath(envelope.payload.get("repo_path", "."))
    versioned_dir = os.path.join(repo, envelope.payload.get("arch_svg_dir", "docs/architecture"))
    os.makedirs(versioned_dir, exist_ok=True)
    stamp = _utc_stamp(envelope)
    versioned = os.path.join(versioned_dir, "{}.svg".format(stamp))
    latest = os.path.join(versioned_dir, "latest.svg")
    new_svg = envelope.payload.get("new_svg", "")
    with open(versioned, "w", encoding="utf-8") as f:
        f.write(new_svg)
    # latest.svg is a copy (not a symlink) so it survives `git add` cleanly on
    # every platform — symlinks across Windows / CI checkouts are fiddly
    with open(latest, "w", encoding="utf-8") as f:
        f.write(new_svg)
    return {"written": True, "versioned_path": versioned, "latest_path": latest,
            "next_step": "git add {} {} && git commit -m 'docs: update architecture diagram ({})'".format(
                versioned, latest, stamp)}


def noop_done(envelope, config) -> Dict[str, Any]:
    """Terminal stage that just confirms the run reached its exit. Used by the
    `done` branch (no drift) and as the carrier for the branch-on-changed
    decision (a branch must live on a node)."""
    return {"ok": True}


# ---------- small private helpers --------------------------------------------

def _top_level_packages(src_dir: str) -> List[str]:
    if not os.path.isdir(src_dir):
        return []
    return [name for name in os.listdir(src_dir)
            if os.path.isdir(os.path.join(src_dir, name))
            and not name.startswith((".", "_", "build", "dist", "node_modules"))
            and os.path.isfile(os.path.join(src_dir, name, "__init__.py"))]


def _python_files(pkg_dir: str) -> List[str]:
    out: List[str] = []
    for root, dirs, files in os.walk(pkg_dir):
        dirs[:] = [d for d in dirs if not d.startswith((".", "_", "__pycache__"))]
        for f in files:
            if f.endswith(".py") and not f.startswith("_"):
                out.append(os.path.join(root, f))
    return out


_DOC_RE = re.compile(r'^\s*"""(.+?)"""', re.DOTALL)


def _module_docstring(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            head = f.read(2000)
    except (OSError, UnicodeDecodeError):
        return ""
    m = _DOC_RE.match(head)
    return m.group(1).strip() if m else ""


def _first_sentence(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[:200]


_IMPORT_RE = re.compile(r"^\s*(?:from\s+(\S+)\s+import|import\s+(\S+))", re.MULTILINE)


def _internal_imports(path: str, internal_packages: List[str]) -> List[str]:
    """Imports whose root segment is one of the project's own top-level
    packages — the dependency edges that matter for an architecture picture."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read(8000)
    except (OSError, UnicodeDecodeError):
        return []
    pkgset = set(internal_packages)
    found: set = set()
    for m in _IMPORT_RE.finditer(text):
        mod = (m.group(1) or m.group(2) or "").split(".")[0]
        if mod in pkgset:
            found.add(mod)
    return sorted(found)


def _utc_stamp(envelope) -> str:
    """UTC timestamp string for the versioned filename. Allow override via
    `_now` in the payload so tests are deterministic."""
    override = envelope.payload.get("_now")
    if override:
        return override
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H%MZ")
