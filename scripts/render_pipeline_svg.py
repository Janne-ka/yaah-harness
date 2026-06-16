#!/usr/bin/env python3
"""render_pipeline_svg — pipeline JSON -> a self-contained SVG of the flow.

Used by: the yaah-pipeline-authoring skill's "draft, show, ask" step — the AI
drafts a pipeline in discussion with the dev, renders it, and the dev reviews
a picture instead of a JSON graph. Also handy for documenting existing configs.
Where: a dev-side script; never imported by the engine.
Why: a flow is judged visually — stage order, gates, fan-outs and branch routes
are exactly what a dev wants to see and exactly what JSON hides.

Layout: BFS layers from `graph.start`, one column per layer, stages stacked
within a column. Edge kinds: `then` (solid), `branch` routes (labeled with the
decision value), `fork` arms (labeled fork), `fanin.expect` (dashed, only when
the arm doesn't already point at the fan-in). `fanout` role barriers are listed
inside the stage box. Stdlib only.

Run: PYTHONPATH=src python3 scripts/render_pipeline_svg.py <pipeline.json> [-o out.svg]
(or rely on the script's own ../src bootstrap when run from anywhere)

Targets Python 3.9+.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from yaah.runtime_factories import _read_json  # `_extends`-aware loader  # noqa: E402
from yaah.validate import is_fork_config  # fork vs role-barrier, single source  # noqa: E402

# stage box geometry; MAXCOL wraps long pipelines serpentine-style into bands
W, H, XGAP, YGAP, PAD, MAXCOL, BANDGAP = 190, 64, 90, 28, 24, 6, 70

# fill/stroke per node type — gates pop, thinking is blue, plumbing is muted
_STYLE = {
    "agent":      ("#dbeafe", "#1d4ed8"),
    "human_gate": ("#fef3c7", "#b45309"),
    "transform":  ("#e5e7eb", "#374151"),
    "render":     ("#dcfce7", "#15803d"),
    "shell":      ("#ede9fe", "#6d28d9"),
    "shell_check": ("#ede9fe", "#6d28d9"),
    "worktree":   ("#ede9fe", "#6d28d9"),
    "get":        ("#fae8ff", "#a21caf"),
    "post":       ("#fae8ff", "#a21caf"),
    "_fork":      ("#fee2e2", "#b91c1c"),   # node-less fork / fan-in stages
    "":           ("#f3f4f6", "#6b7280"),
}


def _esc(s: Any) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _edges(name: str, s: Dict[str, Any], stages: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """(target, label, kind) edges out of one stage. kind: then|branch|fork|expect."""
    out: List[Tuple[str, str, str]] = []
    if s.get("then"):
        out.append((s["then"], "", "then"))
    br = s.get("branch") or {}
    for value, target in (br.get("routes") or {}).items():
        out.append((target, "{}={}".format(br.get("on", "?"), value), "branch"))
    if br.get("default"):
        out.append((br["default"], "default", "branch"))
    if is_fork_config(s, set(stages)):  # explicit `fork` key: targets are STAGE names
        out.extend((t, "fork", "fork") for t in s["fork"])
    return out


def _layers(g: Dict[str, Any]) -> List[List[str]]:
    """BFS layering from start; unreachable stages go in a final column."""
    stages = g["stages"]
    seen, layers, frontier = set(), [], [g["start"]]
    while frontier:
        layer = [n for n in frontier if n not in seen and n in stages]
        if not layer:
            break
        seen.update(layer)
        layers.append(layer)
        nxt: List[str] = []
        for n in layer:
            for t, _, _ in _edges(n, stages[n], stages):
                if t not in seen and t not in nxt and t in stages:
                    nxt.append(t)
        frontier = nxt
    orphans = [n for n in stages if n not in seen]
    if orphans:
        layers.append(orphans)
    return layers


def render(config: Dict[str, Any]) -> str:
    g = config["graph"]
    nodes: Dict[str, Any] = config.get("nodes", {})
    stages: Dict[str, Any] = g["stages"]
    layers = _layers(g)

    # serpentine: chunk layers into bands of MAXCOL columns; each band gets its
    # own vertical strip, offset by the tallest layer in every band above it
    pos: Dict[str, Tuple[int, int]] = {}
    bands = [layers[i:i + MAXCOL] for i in range(0, len(layers), MAXCOL)]
    band_y = PAD
    for band in bands:
        for col, layer in enumerate(band):
            for row, name in enumerate(layer):
                pos[name] = (PAD + col * (W + XGAP), band_y + row * (H + YGAP))
        band_y += max(len(l) for l in band) * (H + YGAP) - YGAP + H // 2 + BANDGAP

    width = PAD * 2 + min(MAXCOL, len(layers)) * (W + XGAP) - XGAP
    height = band_y - BANDGAP - H // 2 + PAD

    parts: List[str] = []
    parts.append(
        '<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        'viewBox="0 0 {w} {h}" font-family="ui-monospace, SFMono-Regular, Menlo, monospace">'
        .format(w=width, h=height))
    parts.append(
        '<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#6b7280"/></marker></defs>')

    # edges first (under the boxes)
    drawn = set()
    for name, s in stages.items():
        if name not in pos:
            continue
        for i, (target, label, kind) in enumerate(_edges(name, s, stages)):
            if target not in pos:
                continue
            drawn.add((name, target))
            parts.append(_edge_svg(pos[name], pos[target], label, dashed=False,
                                   nudge=i * 13))  # stagger labels of sibling edges
        fi = (s.get("fanin") or {})
        for src in fi.get("expect", []):
            if src in pos and (src, name) not in drawn:
                parts.append(_edge_svg(pos[src], pos[name], "expect", dashed=True))

    # stage boxes
    for name, s in stages.items():
        if name not in pos:
            continue
        x, y = pos[name]
        node = s.get("node", "")
        ntype = (nodes.get(node) or {}).get("type", "") if node else "_fork"
        fill, stroke = _STYLE.get(ntype, _STYLE[""])
        parts.append(
            '<rect x="{}" y="{}" rx="8" width="{}" height="{}" fill="{}" stroke="{}" '
            'stroke-width="1.5"/>'.format(x, y, W, H, fill, stroke))
        title = name + ("  ▶" if name == g["start"] else "")
        parts.append('<text x="{}" y="{}" font-size="13" font-weight="bold" fill="{}">{}'
                     '</text>'.format(x + 10, y + 18, stroke, _esc(title)))
        sub: List[str] = []
        if ntype and ntype != "_fork":
            sub.append(ntype)
        elif ntype == "_fork":
            sub.append("fan-in" if s.get("fanin") else "fork")
        fo = s.get("fanout")
        if isinstance(fo, list):
            sub.append("⇉ " + ", ".join(fo))  # role barrier (fork arms are edges)
        if s.get("validators"):
            att = int(s.get("max_attempts", 1))
            mark = "  ↻{}{}".format(att, "+fb" if s.get("feedback") else "") if att > 1 else ""
            sub.append("✓ " + ", ".join(s["validators"]) + mark)
        for i, line in enumerate(sub[:3]):
            parts.append('<text x="{}" y="{}" font-size="10.5" fill="#374151">{}</text>'
                         .format(x + 10, y + 33 + i * 13, _esc(line[:32])))
        if s.get("then") is None and not s.get("branch") and not s.get("fanout"):
            parts.append('<text x="{}" y="{}" font-size="10.5" fill="#9ca3af">■ end</text>'
                         .format(x + W - 44, y + H - 8))

    parts.append("</svg>")
    return "\n".join(parts)


def _edge_svg(a: Tuple[int, int], b: Tuple[int, int], label: str, *, dashed: bool,
              nudge: int = 0) -> str:
    x1, y1 = a[0] + W, a[1] + H // 2
    x2, y2 = b[0], b[1] + H // 2
    if x2 <= x1 and y2 > y1 + H:  # forward into a LOWER band (serpentine wrap)
        x1, y1 = a[0] + W // 2, a[1] + H
        x2, y2 = b[0] + W // 2, b[1]
        path = ('<path d="M {} {} C {} {}, {} {}, {} {}" fill="none" stroke="#6b7280" {} '
                'marker-end="url(#arr)"/>').format(
            x1, y1, x1, y1 + 40, x2, y2 - 40, x2, y2 - 2,
            'stroke-dasharray="5 4"' if dashed else "")
        lx, ly = (x1 + x2) // 2, (y1 + y2) // 2 + nudge
        if label:
            path += ('<text x="{}" y="{}" font-size="10" fill="#b45309" '
                     'text-anchor="middle">{}</text>').format(lx, ly, _esc(label))
        return path
    if x2 <= x1:  # back-edge (e.g. blocked -> earlier column): route below
        x1, y1 = a[0] + W // 2, a[1] + H
        x2, y2 = b[0] + W // 2, b[1] + H
        path = '<path d="M {} {} C {} {}, {} {}, {} {}" fill="none" stroke="#6b7280" {} marker-end="url(#arr)"/>'.format(
            x1, y1, x1, y1 + 36, x2, y2 + 36, x2, y2 + 2,
            'stroke-dasharray="5 4"' if dashed else "")
        lx, ly = (x1 + x2) // 2, max(y1, y2) + 30 + nudge
    else:
        mx = (x1 + x2) // 2
        path = '<path d="M {} {} C {} {}, {} {}, {} {}" fill="none" stroke="#6b7280" {} marker-end="url(#arr)"/>'.format(
            x1, y1, mx, y1, mx, y2, x2 - 2, y2,
            'stroke-dasharray="5 4"' if dashed else "")
        lx, ly = mx, min(y1, y2) + abs(y2 - y1) // 2 - 6 + nudge
    if label:
        path += ('<text x="{}" y="{}" font-size="10" fill="#b45309" text-anchor="middle">{}'
                 '</text>').format(lx, ly, _esc(label))
    return path


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Render a yaah pipeline JSON as SVG.")
    ap.add_argument("pipeline", help="pipeline JSON (._extends resolved)")
    ap.add_argument("-o", "--out", help="output path (default: <pipeline>.svg)")
    ns = ap.parse_args(argv)
    config = _read_json(os.path.abspath(ns.pipeline))
    out = ns.out or os.path.splitext(ns.pipeline)[0] + ".svg"
    with open(out, "w", encoding="utf-8") as f:
        f.write(render(config))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
