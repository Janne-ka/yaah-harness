"""Transforms for the authoring meta-pipeline (examples/author/).

check_config — the VALIDATOR on the draft stage (wired via the stage's
`validators:` list). It runs the engine's OWN author-time checks
(yaah.validate.validate_root / validate_pipeline) over the drafted config and
returns a Verdict envelope. A failed verdict makes the harness retry the draft
stage with the exact validation errors appended to the agent's prompt
(max_attempts + feedback) — the engine's retry loop IS the repair loop.

write_config — the terminal stage: writes the approved config pair
(<name>.json root + <name>-pipeline.json) under an out_dir resolved relative
to THIS file's directory (i.e. beside the config; deterministic regardless of
the caller's cwd). `name` and `out_dir` are payload-derived values reaching
the filesystem, so both are sanitized at this seam (AGENTS.md security note).

Known limitation (documented, deliberate): the validator checks the CONFIG
SHAPE only. Assets the drafted config references (prompt files, templates)
are not generated here — authoring those is a follow-up step for the operator.

Python 3.9 compatible.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List

from yaah.core import Envelope, Failure, Verdict
from yaah.validate import validate_pipeline, validate_root

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")

_FIX_HINT = "fix every named problem and re-emit the FULL corrected JSON object (not a diff)"


def check_config(envelope: Envelope, config: Any) -> Envelope:
    """Verdict-shaped validator over the draft agent's output payload.

    Expects the parsed draft on the payload (parse-by-default merged the
    agent's JSON reply): `name` (kebab-case), `root` (deployment config dict),
    `pipeline` (pipeline config dict). Returns Verdict.passed() or a hard
    Verdict.failed(...) whose failures feed the retry+feedback loop.
    """
    p = envelope.payload
    failures: List[Failure] = []

    name = p.get("name")
    if not (isinstance(name, str) and _NAME_RE.match(name)):
        failures.append(Failure(
            "bad_name",
            "'name' is {!r}; it must be a short kebab-case identifier".format(name),
            "use lowercase letters/digits/hyphens, starting with a letter, e.g. \"summarize-review\""))

    root = p.get("root")
    if not isinstance(root, dict):
        failures.append(Failure(
            "missing_root", "'root' is {} — expected the deployment config object"
            .format(type(root).__name__), _FIX_HINT))
    pipeline = p.get("pipeline")
    if not isinstance(pipeline, dict):
        failures.append(Failure(
            "missing_pipeline", "'pipeline' is {} — expected the pipeline config object"
            .format(type(pipeline).__name__), _FIX_HINT))

    if isinstance(root, dict):
        try:
            validate_root(root)
        except ValueError as e:
            failures.append(Failure("invalid_root", str(e), _FIX_HINT))
    if isinstance(pipeline, dict):
        try:
            # No base_path: the drafted config has no directory yet, so
            # template_file edges are left to the runtime (validate_pipeline
            # documents this degradation).
            validate_pipeline(pipeline)
        except ValueError as e:
            failures.append(Failure("invalid_pipeline", str(e), _FIX_HINT))

    if failures:
        return Verdict.failed(*failures).to_envelope(envelope)
    return Verdict.passed().to_envelope(envelope)


def _safe_out_dir(out_dir: Any) -> str:
    """Resolve the payload's out_dir against this file's directory. Boundary
    check: a payload-derived value reaching the filesystem must not escape the
    example dir (no absolute paths, no '..')."""
    if not isinstance(out_dir, str) or not out_dir:
        out_dir = "generated"
    if os.path.isabs(out_dir) or ".." in out_dir.replace("\\", "/").split("/"):
        raise ValueError(
            "out_dir {!r} must be a relative path inside the example dir "
            "(no absolute paths, no '..')".format(out_dir))
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), out_dir)


def write_config(envelope: Envelope, config: Any) -> Dict[str, Any]:
    """Write the approved config pair to disk. The root's `pipeline` ref is
    rewritten to the actual pipeline filename so the written pair is coherent
    by construction. Returns the payload enriched with the written paths
    (enrich, don't replace — call:'envelope' transforms otherwise drop keys).
    """
    p = envelope.payload
    name = p.get("name")
    if not (isinstance(name, str) and _NAME_RE.match(name)):
        # unreachable when check_config gated the draft; kept because name is
        # a payload-derived value becoming a filename (validate at boundaries)
        raise ValueError("payload 'name' {!r} is not a safe kebab-case filename".format(name))
    dest = _safe_out_dir(p.get("out_dir"))
    os.makedirs(dest, exist_ok=True)

    pipeline_file = "{}-pipeline.json".format(name)
    root = dict(p["root"])
    root["pipeline"] = pipeline_file

    root_path = os.path.join(dest, "{}.json".format(name))
    pipeline_path = os.path.join(dest, pipeline_file)
    for path, obj in ((root_path, root), (pipeline_path, p["pipeline"])):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=False)
            f.write("\n")
    return {**p, "written_root": root_path, "written_pipeline": pipeline_path}
