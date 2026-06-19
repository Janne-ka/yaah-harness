"""Transforms for the config-flow visualizer pipeline.

`snapshot_config_flow` is the new one: takes a target yaah root config path
(from `payload['target_config_path']`), walks its `_extends` chain, follows
the `pipeline:` and `input:` references, resolves every node's `model:` /
`prompt:` / `target:` strings against the effective root's named maps, and
emits a textual description the agent draws into mermaid.

The rest (`parse_extracted`, `render_mermaid`, `write_versioned`,
`noop_done`) are copied near-verbatim from examples/arch-drift/transforms.py
— each example owns its transforms (cross-example imports would be
PYTHONPATH gymnastics). If a third example needs the same trio, that's
when we factor a `yaah-transforms-cookbook` helper module; not before.

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


# ---------- snapshot_config_flow: the new one ---------------------------------

def snapshot_config_flow(envelope, config) -> Dict[str, Any]:
    """Walk a target yaah root config and emit a text snapshot of its flow.

    Required payload key: `target_config_path` (absolute or relative-to-cwd
    path to the root config to visualize).

    The snapshot describes, in order:
      1. The `_extends` chain (base→leaf), one paragraph per layer naming
         what that layer adds or overrides.
      2. The effective (post-merge) root config — what yaah actually sees.
      3. The pipeline referenced by `pipeline:` — its nodes (with each
         node's `model:`/`prompt:`/`target:`/`attach:` string RESOLVED back
         to the effective root's named maps) and its graph (edges with
         then/fork/fanin/branch annotations).
      4. The fixture referenced by `input:` — the keys of the initial payload.

    The agent then draws a flowchart of all that. No assumptions about a
    specific yaah application — works on hello-yaah, review-pipeline,
    arch-drift, or any user-authored root config."""
    target = envelope.payload.get("target_config_path")
    if not target:
        raise ValueError(
            "snapshot_config_flow requires `target_config_path` in payload "
            "(absolute or relative-to-cwd path to a yaah root config JSON)")
    target_abs = os.path.abspath(target)
    if not os.path.isfile(target_abs):
        raise FileNotFoundError(
            "target_config_path {!r} does not exist (resolved to {})".format(
                target, target_abs))

    lines: List[str] = [
        "# Config-flow snapshot",
        "# target: {}".format(target_abs),
        "# strategy: snapshot_config_flow",
        ""]

    # 1) walk _extends (leaf-first), abort on cycles or packaged-seed refs we
    # don't try to follow into the wheel.
    chain: List[tuple] = []
    cur = target_abs
    seen: set = set()
    while cur and cur not in seen:
        seen.add(cur)
        try:
            with open(cur, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            lines.append("# WARN: could not load {} ({}); chain truncated".format(cur, e))
            break
        chain.append((cur, raw))
        ext = raw.get("_extends") if isinstance(raw, dict) else None
        if not ext:
            break
        if isinstance(ext, str) and ext.startswith("yaah:"):
            lines.append("# (chain ends at packaged seed {!r}; not walked)".format(ext))
            break
        cur = ext if os.path.isabs(ext) else os.path.normpath(
            os.path.join(os.path.dirname(cur), ext))

    # 2) per-layer description (base-first so reading order matches resolution)
    lines.append("## _extends chain (base → leaf, what each layer contributes)")
    for i, (path, raw) in enumerate(reversed(chain)):
        rel = os.path.basename(path)
        lines.append("{}. {}".format(i + 1, rel))
        if isinstance(raw.get("_doc"), str):
            lines.append("   purpose: {}".format(_first_sentence(raw["_doc"])))
        for k, v in raw.items():
            if k.startswith("_"):
                continue
            lines.append("   - {}: {}".format(k, _summarize(v)))
        lines.append("")

    # 3) effective config (deep merge per RFC 7396 JSON Merge Patch)
    effective: Dict[str, Any] = {}
    for _, raw in reversed(chain):
        effective = _deep_merge(effective, {k: v for k, v in raw.items() if not k.startswith("_")})

    lines.append("## effective root (post-merge — what yaah actually sees)")
    for k in sorted(effective):
        lines.append("- {}: {}".format(k, _summarize(effective[k], max_len=200)))
    lines.append("")

    # 4) describe the referenced pipeline
    pipeline_ref = effective.get("pipeline")
    pipeline_spec: Optional[Dict[str, Any]] = None
    if isinstance(pipeline_ref, str):
        leaf_dir = os.path.dirname(target_abs)
        pipeline_path = pipeline_ref if os.path.isabs(pipeline_ref) else os.path.normpath(
            os.path.join(leaf_dir, pipeline_ref))
        try:
            with open(pipeline_path, "r", encoding="utf-8") as f:
                pipeline_spec = json.load(f)
            lines.append("## pipeline ({})".format(pipeline_ref))
            if isinstance(pipeline_spec.get("_doc"), str):
                lines.append("   purpose: {}".format(_first_sentence(pipeline_spec["_doc"])))
            providers = effective.get("providers") or {}
            prompt_sources = effective.get("prompt_sources") or {}
            data_sources = effective.get("data_sources") or {}
            data_sinks = effective.get("data_sinks") or {}
            mcp_sources = effective.get("mcp_sources") or {}
            nodes = pipeline_spec.get("nodes") or {}
            lines.append("nodes (with reference resolution):")
            for role, node in nodes.items():
                if role.startswith("_") or not isinstance(node, dict):
                    continue
                t = node.get("type", "?")
                refs: List[str] = []
                if node.get("model"):
                    refs.append(_resolve_ref("model", node["model"], providers))
                if node.get("prompt"):
                    refs.append(_resolve_ref("prompt", node["prompt"], prompt_sources))
                if node.get("source"):
                    refs.append(_resolve_ref("source", node["source"], data_sources))
                if node.get("sink"):
                    refs.append(_resolve_ref("sink", node["sink"], data_sinks))
                if node.get("mcp") and isinstance(node["mcp"], str):
                    refs.append(_resolve_ref("mcp", node["mcp"], mcp_sources))
                if node.get("target"):
                    refs.append("target={}".format(node["target"]))
                if node.get("attach"):
                    refs.append("attach=[{}]".format(",".join(node["attach"])))
                if node.get("awaiting"):
                    matched = (effective.get("decisions") or {}).get(node["awaiting"])
                    refs.append("awaiting={}{}".format(
                        node["awaiting"],
                        " → matched by decisions block (auto-resolve)" if matched else ""))
                line = "  - {} ({})".format(role, t)
                if refs:
                    line += " | " + " ; ".join(refs)
                lines.append(line)
            graph = pipeline_spec.get("graph") or {}
            lines.append("graph (start: {}):".format(graph.get("start", "?")))
            for name, stage in (graph.get("stages") or {}).items():
                if not isinstance(stage, dict):
                    continue
                edges: List[str] = []
                if stage.get("then"):
                    edges.append("then={}".format(stage["then"]))
                if "fork" in stage:
                    edges.append("fork=[{}]".format(",".join(stage["fork"] or [])))
                if "fanin" in stage and isinstance(stage["fanin"], dict):
                    edges.append("fanin=[{}]".format(",".join(stage["fanin"].get("expect", []) or [])))
                if "branch" in stage and isinstance(stage["branch"], dict):
                    routes = stage["branch"].get("routes", {}) or {}
                    edges.append("branch=[{}]".format(
                        ",".join("{}→{}".format(k, v) for k, v in routes.items())))
                lines.append("  {} → {}".format(name, " | ".join(edges) or "<end>"))
        except (OSError, ValueError) as e:
            lines.append("# WARN: could not load pipeline {} ({})".format(pipeline_ref, e))
        lines.append("")

    # 5) describe the fixture (initial payload)
    input_ref = effective.get("input")
    if isinstance(input_ref, str):
        leaf_dir = os.path.dirname(target_abs)
        input_path = input_ref if os.path.isabs(input_ref) else os.path.normpath(
            os.path.join(leaf_dir, input_ref))
        try:
            with open(input_path, "r", encoding="utf-8") as f:
                fixture = json.load(f)
            lines.append("## fixture ({})".format(input_ref))
            if isinstance(fixture.get("_doc"), str):
                lines.append("   purpose: {}".format(_first_sentence(fixture["_doc"])))
            for k, v in fixture.items():
                if k.startswith("_"):
                    continue
                lines.append("  - {}: {}".format(k, _summarize(v, max_len=200)))
            start = (pipeline_spec or {}).get("graph", {}).get("start", "?")
            lines.append("  → becomes the first envelope's payload at stage '{}'".format(start))
        except (OSError, ValueError) as e:
            lines.append("# WARN: could not load fixture {} ({})".format(input_ref, e))
    elif isinstance(input_ref, dict):
        lines.append("## fixture (inline dict in root):")
        for k, v in input_ref.items():
            lines.append("  - {}: {}".format(k, _summarize(v, max_len=200)))

    return {**envelope.payload, "snapshot": "\n".join(lines), "feedback": ""}


def _resolve_ref(kind: str, ref: str, registry: Dict[str, Any]) -> str:
    """Format a node's `<scheme>:<key>` reference, with the registry hit shown."""
    if not isinstance(ref, str) or ":" not in ref:
        return "{}={}".format(kind, ref)
    scheme, _, _ = ref.partition(":")
    entry = (registry or {}).get(scheme)
    if not isinstance(entry, dict):
        return "{}={} → {}.{} (UNRESOLVED)".format(kind, ref, kind, scheme)
    t = entry.get("type", "?")
    return "{}={} → providers.{} ({})".format(kind, ref, scheme, t) if kind == "model" \
        else "{}={} → {}_sources.{} ({})".format(kind, ref, kind, scheme, t)


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """RFC 7396 JSON Merge Patch: child overrides; nested dicts merge; child
    `null` deletes the key. Same semantics as yaah's `_extends` resolver."""
    if not isinstance(patch, dict):
        return patch
    out = dict(base) if isinstance(base, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _summarize(v: Any, max_len: int = 120) -> str:
    """Compact JSON-ish summary, truncated to max_len."""
    try:
        s = v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(v)
    if len(s) > max_len:
        s = s[:max_len] + "…"
    return s


def _first_sentence(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[:200]


# ---------- copied from arch-drift (parse + render + write + noop) ------------

def parse_extracted(envelope, config) -> Dict[str, Any]:
    """Parse the agent's raw reply as JSON, spreading {mermaid, notes} onto
    the payload. Uses `extract_json` (fence/prose-tolerant) because real
    sonnet/haiku wrap JSON in markdown fences — strict `json.loads` would
    fail on real-mode runs."""
    from yaah.jsonio import extract_json
    raw = envelope.payload.get("raw", "{}")
    obj = extract_json(raw)
    return {**envelope.payload, "mermaid": obj.get("mermaid", ""),
            "notes": obj.get("notes", "")}


_CANNED_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="120">'
    '<rect x="10" y="10" width="80" height="40" fill="#eef"/>'
    '<rect x="120" y="10" width="80" height="40" fill="#eef"/>'
    '<rect x="230" y="10" width="80" height="40" fill="#eef"/>'
    '<text x="50" y="35" text-anchor="middle">root</text>'
    '<text x="160" y="35" text-anchor="middle">pipeline</text>'
    '<text x="270" y="35" text-anchor="middle">fixture</text>'
    '</svg>'
)


def render_mermaid(envelope, config) -> Dict[str, Any]:
    """Render `mermaid` to SVG. Shells out to `mmdc` (mermaid-cli) by default;
    `MERMAID_RENDERER=:canned` returns a fixed canned SVG for offline tests."""
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
        result = subprocess.run([renderer, "-i", mmd, "-o", svg, "-q"],
                                capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError("mmdc failed (exit {}): {}".format(
                result.returncode, result.stderr.strip()[:500]))
        with open(svg, "r", encoding="utf-8") as f:
            return {**envelope.payload, "new_svg": f.read()}


def write_versioned(envelope, config) -> Dict[str, Any]:
    """Write the approved SVG to the path the fixture configured (with a
    UTC-stamped versioned sibling). Prints both paths on stderr at exit so
    the operator doesn't have to read the RESULT envelope to find them."""
    repo = os.path.abspath(envelope.payload.get("repo_path", "."))
    versioned_dir = os.path.join(repo, envelope.payload.get("arch_svg_dir", "diagrams"))
    os.makedirs(versioned_dir, exist_ok=True)
    stamp = _utc_stamp(envelope)
    aspath = envelope.payload.get("arch_svg_path") or ""
    latest_name = os.path.basename(aspath) if aspath else "config-flow.svg"
    stem, _ = os.path.splitext(latest_name)
    versioned = os.path.join(versioned_dir, "{}_{}.svg".format(stem, stamp))
    latest = os.path.join(versioned_dir, latest_name)
    new_svg = envelope.payload.get("new_svg", "")
    with open(versioned, "w", encoding="utf-8") as f:
        f.write(new_svg)
    with open(latest, "w", encoding="utf-8") as f:
        f.write(new_svg)
    next_step = "open {}".format(latest)
    print("\nSVG landed:\n  {}\n  {}\nNext: {}".format(
        latest, versioned, next_step), file=sys.stderr)
    return {"written": True, "versioned_path": versioned,
            "latest_path": latest, "next_step": next_step}


def noop_done(envelope, config) -> Dict[str, Any]:
    return {"ok": True}


def _utc_stamp(envelope) -> str:
    """UTC timestamp for the versioned filename. Override via payload `_now`
    for deterministic tests."""
    override = envelope.payload.get("_now")
    if override:
        return override
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H%MZ")
