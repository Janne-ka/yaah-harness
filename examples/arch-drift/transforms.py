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
import sys
import tempfile
from typing import Any, Dict, List, Optional

from yaah.agents.attacher import Attacher  # ADR-0003 — engine ships zero built-ins;
                                            # this file holds the reference `usage` impl.


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


def snapshot_pipeline_json(envelope, config) -> Dict[str, Any]:
    """Snapshot strategy for pipeline-shaped projects (where the architecture
    IS the pipeline graph, e.g. arch-drift itself). Reads every `*-pipeline.json`
    in `repo_path`, describes the node types + graph edges as text. Treats
    yaah-side imports as a single external dependency (the agent's prompt
    handles the "draw yaah as a black box" framing).

    Use case: pointing arch-drift at itself. The default `snapshot_imports`
    walks Python packages under `src/`; arch-drift has no packages, just one
    transforms.py + JSON files, so imports-walking returns nothing useful."""
    repo = os.path.abspath(envelope.payload.get("repo_path", "."))
    lines: List[str] = [
        "# Pipeline-graph snapshot for architecture extraction",
        "# root: {}".format(os.path.basename(repo)),
        "# strategy: snapshot_pipeline_json",
        "# (yaah is the harness this runs on — represent it as a single",
        "#  external 'yaah' dependency, not as internal architecture)",
        ""]
    try:
        files = sorted(f for f in os.listdir(repo) if f.endswith("-pipeline.json"))
    except OSError:
        files = []
    for pf in files:
        path = os.path.join(repo, pf)
        try:
            with open(path, "r", encoding="utf-8") as f:
                spec = json.load(f)
        except (OSError, ValueError):
            continue
        lines.append("## pipeline: {}".format(pf))
        if isinstance(spec.get("_doc"), str):
            lines.append("  purpose: {}".format(_first_sentence(spec["_doc"])))
        nodes = spec.get("nodes") or {}
        lines.append("  nodes:")
        for role, node in nodes.items():
            if role.startswith("_") or not isinstance(node, dict):
                continue
            t = node.get("type", "?")
            extras: List[str] = []
            if node.get("target"):
                extras.append("target={}".format(node["target"]))
            if node.get("model"):
                extras.append("model={}".format(node["model"]))
            if node.get("attach"):
                extras.append("attach={}".format(",".join(node["attach"])))
            lines.append("    - {} ({}) {}".format(role, t, " ".join(extras)).rstrip())
        stages = ((spec.get("graph") or {}).get("stages")) or {}
        if stages:
            lines.append("  graph (start: {}):".format(
                (spec.get("graph") or {}).get("start", "?")))
            for name, st in stages.items():
                if not isinstance(st, dict):
                    continue
                edge_bits: List[str] = []
                if st.get("then"):
                    edge_bits.append("then={}".format(st["then"]))
                if "fork" in st:
                    edge_bits.append("fork=[{}]".format(",".join(st["fork"] or [])))
                if "fanin" in st and isinstance(st["fanin"], dict):
                    edge_bits.append("fanin=[{}]".format(",".join(st["fanin"].get("expect", []) or [])))
                if "branch" in st and isinstance(st["branch"], dict):
                    routes = st["branch"].get("routes", {}) or {}
                    edge_bits.append("branch=[{}]".format(
                        ",".join("{}->{}".format(k, v) for k, v in routes.items())))
                lines.append("    {} -> {}".format(name, " | ".join(edge_bits) or "<end>"))
        lines.append("")
    return {**envelope.payload, "snapshot": "\n".join(lines), "feedback": ""}


# Strategy dispatcher: the pipeline's snapshot stage points here; the fixture
# selects the strategy via the `snapshot_strategy` payload key (default:
# "imports"). One pipeline, multiple targets via input.json — no second
# pipeline file needed.
_SNAPSHOT_STRATEGIES = {
    "imports": snapshot_imports,
    "pipeline_json": snapshot_pipeline_json,
}


def snapshot(envelope, config) -> Dict[str, Any]:
    """Dispatch to the snapshot strategy named in `payload['snapshot_strategy']`
    (default: 'imports'). Lets one pipeline serve multiple targets — the
    fixture decides what to snapshot, the pipeline JSON doesn't change."""
    name = envelope.payload.get("snapshot_strategy", "imports")
    fn = _SNAPSHOT_STRATEGIES.get(name)
    if fn is None:
        raise ValueError(
            "unknown snapshot_strategy {!r}; known: {}".format(
                name, sorted(_SNAPSHOT_STRATEGIES)))
    return fn(envelope, config)


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
    payload. Uses yaah's `extract_json` (fence/prose-tolerant — strips ```json
    fences and finds the first balanced JSON object) because real LLMs often
    wrap their JSON output in explanatory prose or markdown fences. The
    validator (`json_object`, required: ['mermaid']) has already confirmed the
    keys are present; this lifts them into real payload slots so
    `render_mermaid` and the report template can read them."""
    from yaah.jsonio import extract_json
    raw = envelope.payload.get("raw", "{}")
    obj = extract_json(raw)
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
    new_svg = envelope.payload.get("new_svg", "")
    # The "latest" file: by default `latest.svg` (single-target convention from
    # the A-only example). When `arch_svg_path` names a specific file (the
    # multi-target setup — e.g. dog-food's `arch-drift-only.svg` vs
    # `yaah-with-arch-drift.svg`), write to THAT filename instead so each
    # target keeps its own canonical pointer in the same dir.
    aspath = envelope.payload.get("arch_svg_path") or ""
    latest_name = os.path.basename(aspath) if aspath else "latest.svg"
    stem, _ = os.path.splitext(latest_name)
    versioned = os.path.join(versioned_dir, "{}-{}.svg".format(stem, stamp))
    latest = os.path.join(versioned_dir, latest_name)
    with open(versioned, "w", encoding="utf-8") as f:
        f.write(new_svg)
    # latest is a copy (not a symlink) so it survives `git add` cleanly on
    # every platform — symlinks across Windows / CI checkouts are fiddly
    with open(latest, "w", encoding="utf-8") as f:
        f.write(new_svg)
    return {"written": True, "versioned_path": versioned, "latest_path": latest,
            "next_step": "git add {} {} && git commit -m 'docs: update architecture diagram ({})'".format(
                versioned, latest, latest_name)}


def noop_done(envelope, config) -> Dict[str, Any]:
    """Terminal stage that just confirms the run reached its exit. Used by the
    `done` branch (no drift) and as the carrier for the branch-on-changed
    decision (a branch must live on a node)."""
    return {"ok": True}


# ---------- A/B variant: usage attacher + fanin reducer + per-candidate ops --

class UsageAttacher(Attacher):
    """Reference `usage` attacher implementation (ADR-0003 — engine ships
    zero built-ins; the canonical implementation lives here for consumers
    to copy into their own transforms file).

    Reads the tracer's most recent model_call span for the current
    correlation. Returns the projected cost data (tokens + model) under
    payload key `usage`. Dollar conversion is NOT here — yaah deliberately
    externalizes pricing via the aggregator's price-map (see
    src/yaah/trace/contributors/cost.py:8-9). Downstream code that wants $
    multiplies tokens by a config-supplied price-map (the A/B report
    template does this via a `prices` key)."""

    name = "usage"
    requires_capture = ("cost",)

    def attach(self, envelope, span):
        if not span:
            return {}
        return {"usage": {
            "tokens_in": span.get("tokens_in", 0),
            "tokens_out": span.get("tokens_out", 0),
            "model": span.get("model"),
        }}


def merge_candidates(arrived: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Fanin reducer for the A/B fork. `arrived` is `{branch_id: payload}`
    keyed by the FORK BRANCH NAMES (not the last-stage names) — for our
    pipeline that's `extract-a` and `extract-b`. Builds a sorted
    `candidates` list with the per-branch facts (label, mermaid, notes,
    usage, new_svg) and carries the shared state (committed_svg, repo_path,
    etc.) forward by lifting it from the first arrived payload."""
    candidates: List[Dict[str, Any]] = []
    shared: Dict[str, Any] = {}
    per_branch_keys = {"mermaid", "notes", "usage", "new_svg", "raw"}
    for branch_id, payload in arrived.items():
        # branch_ids look like "extract-a" / "extract-b"; strip to "a"/"b"
        label = branch_id.rsplit("-", 1)[-1]
        candidates.append({
            "label": label,
            "mermaid": payload.get("mermaid", ""),
            "notes": payload.get("notes", ""),
            "usage": payload.get("usage") or {},
            "new_svg": payload.get("new_svg", ""),
        })
        if not shared:
            shared = {k: v for k, v in payload.items()
                      if k not in per_branch_keys}
    candidates.sort(key=lambda c: c["label"])
    return {**shared, "candidates": candidates}


def prepare_ab_template(envelope, config) -> Dict[str, Any]:
    """Flatten `candidates` into per-candidate template keys (the A/B report
    template uses `{{candidate_a_svg}}` / `{{candidate_b_tokens}}` etc.
    because yaah's render is mustache-style with no loops). Also computes
    approximate dollar cost from `tokens_in/tokens_out` + a `prices` config
    block (per-million pricing; multiplication done here so the engine
    stays out of pricing — see ADR-0003 "dollar cost stays in price-map")."""
    candidates = envelope.payload.get("candidates", [])
    # `prices`: map model name -> {"in": $/M tokens, "out": $/M tokens}.
    # Configured per-pipeline (the local config sets a stub map; real
    # config supplies the right rates).
    prices = (config.extras or {}).get("prices", {})
    out: Dict[str, Any] = {**envelope.payload}
    for c in candidates:
        label = c["label"]
        model = (c.get("usage") or {}).get("model") or "?"
        tin = (c.get("usage") or {}).get("tokens_in", 0) or 0
        tout = (c.get("usage") or {}).get("tokens_out", 0) or 0
        rate = prices.get(model, {"in": 0.0, "out": 0.0})
        cost_usd = round((tin * rate.get("in", 0.0) + tout * rate.get("out", 0.0)) / 1_000_000, 6)
        out["candidate_{}_label".format(label)] = label
        out["candidate_{}_model".format(label)] = model
        out["candidate_{}_tokens_in".format(label)] = str(tin)
        out["candidate_{}_tokens_out".format(label)] = str(tout)
        out["candidate_{}_cost".format(label)] = "${:.6f}".format(cost_usd) if cost_usd else "$0.00"
        out["candidate_{}_svg".format(label)] = c.get("new_svg", "")
        out["candidate_{}_notes".format(label)] = c.get("notes", "")
    return out


def write_candidate(envelope, config) -> Dict[str, Any]:
    """Write the SVG for the chosen candidate. `config.extras["which"]` is
    "a" or "b" (set per stage in the pipeline JSON's `land-a` / `land-b`).
    Filename embeds both timestamp AND label so the versioned dir keeps
    the A/B history disambiguated. Does NOT git-commit — prints the
    suggested `git add && git commit` line like write_versioned does."""
    which = (config.extras or {}).get("which")
    if which not in ("a", "b"):
        raise ValueError("write_candidate config.which must be 'a' or 'b' "
                         "(got {!r})".format(which))
    candidates = envelope.payload.get("candidates", [])
    chosen = next((c for c in candidates if c["label"] == which), None)
    if chosen is None:
        raise ValueError("no candidate with label {!r} in payload".format(which))
    repo = os.path.abspath(envelope.payload.get("repo_path", "."))
    versioned_dir = os.path.join(repo, envelope.payload.get("arch_svg_dir", "docs/architecture"))
    os.makedirs(versioned_dir, exist_ok=True)
    stamp = _utc_stamp(envelope)
    # filename gets the model name AFTER the provider prefix (e.g. "claude:claude-sonnet-4-6"
    # -> "claude-sonnet-4-6"), with any remaining filesystem-unfriendly chars normalized
    model_full = (chosen.get("usage") or {}).get("model") or "model"
    model_short = model_full.rsplit(":", 1)[-1].replace("/", "-")
    versioned = os.path.join(versioned_dir, "{}-{}-{}.svg".format(stamp, which, model_short))
    latest = os.path.join(versioned_dir, "latest.svg")
    new_svg = chosen.get("new_svg", "")
    with open(versioned, "w", encoding="utf-8") as f:
        f.write(new_svg)
    with open(latest, "w", encoding="utf-8") as f:
        f.write(new_svg)
    return {**envelope.payload, "written": True, "versioned_path": versioned,
            "latest_path": latest, "chose": which,
            "next_step": "git add {} {} && git commit -m 'docs: update architecture diagram (candidate {} / {})'".format(
                versioned, latest, which, model_short)}


def revise_exit(envelope, config) -> Dict[str, Any]:
    """Terminal stage for the A/B `revise` decision (v1 — the revise loop is
    deferred per the plan; v1 just surfaces the feedback for the operator).
    Prints to stderr so it's visible without parsing the run's RESULT line."""
    feedback = envelope.payload.get("feedback", "")
    print("REVISE: human asked for a revision. feedback: {!r}".format(feedback),
          file=sys.stderr)
    print("v1 of the A/B pipeline does not loop on revise; re-run the pipeline "
          "with the feedback incorporated into your prompt or input.",
          file=sys.stderr)
    return {**envelope.payload, "revised": True, "feedback": feedback}


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
