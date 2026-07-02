"""build_schemas — generate JSON Schemas for root + pipeline configs from
the engine's own validation tables.

Used by: contributors regenerating `schemas/*.schema.json` after touching
`src/yaah/validate.py` or the factory tables in `src/yaah/runtime_factories.py`.
Wired into the test suite via `tests/test_schemas_drift.py` (running this
script must produce output matching the committed schemas — catches drift
at suite time).

Where: a small build helper, sibling of `scripts/build_catalog.py` which
auto-generates `docs/module-catalog.md` from the same engine tables. Both
follow the "the code is the truth; the doc/schema is derived" pattern.

Why: end-users (and AI coding agents) authoring YAAH configs in VS Code /
JetBrains / Cursor get autocomplete + error highlighting for free when the
config file references the schema via `$schema:`. Removes the why-not §1.3
"stringly-typed magic strings with no IDE help" complaint without forcing
the user to memorize the surface.

Run: `python3 scripts/build_schemas.py`
Writes: `schemas/root.schema.json`, `schemas/pipeline.schema.json`
"""
from __future__ import annotations

import json
import os
import sys
from typing import List, Tuple

# Make the engine importable when running from the repo root.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

# The schema-derivation logic lives IN the package (yaah.schema_gen) so it can
# also run from an installed wheel at `yaah init` time. This script keeps only
# the repo-root file-writing + drift surface; it re-exports the two builders so
# `tests/test_schemas_drift.py` (which imports them off this module) is unchanged.
from yaah.schema_gen import build_pipeline_schema, build_root_schema   # noqa: E402,F401


def write_schemas() -> List[Tuple[str, str]]:
    out_dir = os.path.join(ROOT, "schemas")
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for name, schema in (
        ("root.schema.json",     build_root_schema()),
        ("pipeline.schema.json", build_pipeline_schema()),
    ):
        path = os.path.join(out_dir, name)
        text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        written.append((name, path))
    return written


def main() -> None:
    for name, path in write_schemas():
        print("wrote {} ({} bytes)".format(name, os.path.getsize(path)))


if __name__ == "__main__":
    main()
