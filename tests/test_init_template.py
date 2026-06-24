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

from yaah.init_template import ARCHETYPES, STARTER_TEMPLATE, scaffold
from yaah.validate import validate_root, validate_pipeline


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


def scenario_scaffold_every_archetype() -> None:
    """Each registered archetype must scaffold cleanly: every declared file
    lands on disk, the produced starter root + pipeline validate, and no
    archetype shares its target directory with another (the FileExistsError
    refusal still bites the second scaffold)."""
    for name, template in ARCHETYPES.items():
        tmp = tempfile.mkdtemp(prefix="yaah-arch-{}-".format(name))
        try:
            target = os.path.join(tmp, "p")
            n = scaffold(target, name)
            assert n == len(template), (name, n, len(template))

            # every declared file landed on disk with its declared content
            for rel, expected in template.items():
                got = _read(os.path.join(target, rel))
                assert got == expected, "scaffold corrupted {} in {}".format(rel, name)

            # the produced root + pipeline both validate — proves the embed
            # isn't lying about shape (caught at scaffold time, not by a
            # confused user running it for the first time)
            root_path = os.path.join(target, "starter.local.json")
            with open(root_path, "r") as f:
                root = json.load(f)
            validate_root(root)
            pipeline_path = os.path.join(target, root["pipeline"])
            with open(pipeline_path, "r") as f:
                pipeline = json.load(f)
            validate_pipeline(pipeline)

            # second scaffold into the same non-empty dir must refuse
            try:
                scaffold(target, name)
            except FileExistsError:
                pass
            else:
                raise AssertionError(
                    "{}: scaffold must refuse a non-empty target dir".format(name))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print("PASS every archetype ({}) scaffolds + validates".format(
        ", ".join(sorted(ARCHETYPES))))


def scenario_scaffold_unknown_archetype() -> None:
    """An unknown archetype raises ValueError with an actionable message
    that lists the known names (per the error-voice rule)."""
    tmp = tempfile.mkdtemp(prefix="yaah-arch-unknown-")
    try:
        target = os.path.join(tmp, "p")
        try:
            scaffold(target, "no-such-archetype")
        except ValueError as e:
            msg = str(e)
            assert "no-such-archetype" in msg, msg
            assert "linear" in msg, msg  # the list of known names is shown
            assert "docs/archetypes.md" in msg, msg  # pointer to the deeper doc
        else:
            raise AssertionError("scaffold should refuse an unknown archetype")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS scaffold rejects unknown archetypes with an actionable message")


if __name__ == "__main__":
    main()
    scenario_scaffold_every_archetype()
    scenario_scaffold_unknown_archetype()
