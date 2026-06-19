"""The `yaah init <dir>` scaffold.

What it proves: scaffold writes the embedded starter into a fresh directory,
the produced `starter.local.json` passes `validate_root`, repeating into a
non-empty directory raises (no silent clobber), and the embedded content stays
identical to `examples/hello-yaah/` (drift caught at test time, not by a
confused user). Usability-gaps #1 / §3.

Run: cd yaah && PYTHONPATH=src python3 tests/test_init_template.py

Targets Python 3.9+.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile

from yaah.init_template import STARTER_TEMPLATE, scaffold
from yaah.validate import validate_root


HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.normpath(os.path.join(HERE, "..", "examples", "hello-yaah"))


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="yaah-init-")
    try:
        target = os.path.join(tmp, "my-pipeline")
        n = scaffold(target)
        assert n == len(STARTER_TEMPLATE), (n, len(STARTER_TEMPLATE))

        # every declared file landed on disk with its declared content
        for rel, expected in STARTER_TEMPLATE.items():
            got = _read(os.path.join(target, rel))
            assert got == expected, "scaffold corrupted {}".format(rel)

        # the produced root validates — proves the embed isn't lying about shape
        with open(os.path.join(target, "starter.local.json"), "r") as f:
            root = json.load(f)
        validate_root(root)

        # second scaffold into the same non-empty dir must refuse
        try:
            scaffold(target)
        except FileExistsError:
            pass
        else:
            raise AssertionError("scaffold must refuse a non-empty target dir")

        # scaffolding into an empty pre-existing dir is fine
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty)
        scaffold(empty)

        # drift check: the embed must mirror examples/hello-yaah exactly.
        # If you intentionally update one, update the other in the same commit.
        for rel, expected in STARTER_TEMPLATE.items():
            example_path = os.path.join(EXAMPLE, rel)
            assert os.path.exists(example_path), \
                "embed has {} but example doesn't — drift".format(rel)
            on_disk = _read(example_path)
            assert on_disk == expected, \
                "drift in {}: embed and examples/hello-yaah disagree".format(rel)

        print("PASS yaah init scaffolds a valid starter; mirrors examples/hello-yaah")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
