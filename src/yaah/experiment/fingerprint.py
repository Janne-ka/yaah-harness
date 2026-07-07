"""config_fingerprint — the variant-identity hash on every experiment row (AB-1).

Used by: the `yaah ab` runner (stamps each row) and the comparison report
(groups populations by it).
Where: yaah.experiment — experiment-layer identity, not engine state.
Why: a campaign ITERATES — the author edits a variant mid-campaign and keeps
collecting. Rows must group by what ACTUALLY ran, not by the variant's name,
or old-B and new-B silently mix into one population (the design eval's
population-mixing trap). Behavior lives in three places, all hashed:

  1. the EFFECTIVE root config (post-merge), minus per-run data (`input` — an
     experiment varies inputs per row; they are recorded on the row itself),
  2. the EFFECTIVE pipeline (file via the same `_extends`-resolving reader the
     runtime uses, or the inline dict),
  3. the BYTES of every file the pipeline's prompts render from: file-sourced
     `prompt:` refs (prompts live in files — the project position — so a
     prompt edit IS a config change) and render `template_file`s.

Honest limits, by design — behavior-bearing INPUTS the fingerprint does NOT
hash (know these before trusting a population split): non-file prompt sources
(http/langfuse — only their REF is hashed; pin remote prompts for campaigns),
fake/scripted provider FIXTURE file bytes, plugin/`fn:` transform CODE, and
tool scripts. Config changes to any of those still change the fingerprint via
the config; edits to the referenced files' CONTENTS do not. Missing files
hash as MISSING markers rather than raising: the fingerprint's job is
identity, not validation — validate_config owns that.

Targets Python 3.9+.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Tuple


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _prompt_files(root: Dict[str, Any], pipeline: Dict[str, Any],
                  base: str) -> List[str]:
    """Every file path the pipeline's prompts/templates read from, resolved the
    way the runtime resolves them: `prompt: "src:key"` (or bare key via
    default_prompt_source) against a type=file source's dir + ext; render
    `template_file` against the root's base dir."""
    sources = root.get("prompt_sources") or {}
    default_src = root.get("default_prompt_source")
    paths: List[str] = []
    for node in (pipeline.get("nodes") or {}).values():
        if not isinstance(node, dict):
            continue
        ref = node.get("prompt")
        if isinstance(ref, str) and ref:
            src_name, sep, key = ref.partition(":")
            if not sep:
                src_name, key = str(default_src), ref
            src = sources.get(src_name) or {}
            if src.get("type") == "file" and isinstance(key, str) and key:
                # defaults MUST match _build_prompt_source (runtime_factories):
                # dir="prompts", ext=".md" — a diverging default here hashed the
                # wrong path and made prompt edits invisible (eval catch F1)
                d = src.get("dir", "prompts")
                d = d if os.path.isabs(d) else os.path.join(base, d)
                key_path = key if os.path.isabs(key) else key + src.get("ext", ".md")
                paths.append(os.path.join(d, key_path))
        tfile = node.get("template_file")
        if isinstance(tfile, str) and tfile:
            paths.append(tfile if os.path.isabs(tfile) else os.path.join(base, tfile))
    return sorted(set(paths))


def config_fingerprint(root: Dict[str, Any], base: str) -> str:
    """SHA-256 hex over the variant's full behavior surface (see module doc).
    `root` is the EFFECTIVE root (already merged if it came from overlays);
    `base` is the directory its relative paths resolve against."""
    from ..runtime_factories import _read_json, _rel

    ident = {k: v for k, v in root.items() if k != "input"}
    pipeline_ref = root.get("pipeline")
    if isinstance(pipeline_ref, dict):
        pipeline: Dict[str, Any] = pipeline_ref
    elif isinstance(pipeline_ref, str) and pipeline_ref:
        try:
            pipeline = _read_json(_rel(base, pipeline_ref))
        except OSError:
            pipeline = {"__missing_pipeline__": pipeline_ref}
    else:
        pipeline = {}

    h = hashlib.sha256()
    h.update(b"root\0")
    h.update(_canon(ident))
    h.update(b"pipeline\0")
    h.update(_canon(pipeline))
    for path in _prompt_files(root, pipeline, base):
        h.update(b"file\0" + path.encode("utf-8") + b"\0")
        try:
            with open(path, "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(b"__MISSING__")   # identity, not validation — stay total
    return h.hexdigest()
