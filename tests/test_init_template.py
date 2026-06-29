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

from yaah import schema_gen
from yaah.init_template import ARCHETYPES, load_template, scaffold, _schema_targets
from yaah.validate import validate_root, validate_pipeline


def _assert_scaffolded_matches(target: str, template: dict) -> None:
    """Every NON-config file lands verbatim; every config file lands verbatim
    EXCEPT a `$schema` pointer injected as the first key. The generated schemas
    land under schemas/ and match the engine's own generator (generate-at-scaffold,
    so the autocomplete always matches the installed engine)."""
    targets = _schema_targets(template)
    for rel, expected in template.items():
        got = _read(os.path.join(target, rel))
        if rel in targets:
            assert json.loads(got).get("$schema") == "schemas/" + targets[rel], \
                "scaffold did not inject the right $schema into {}".format(rel)
        else:
            assert got == expected, "scaffold corrupted {}".format(rel)
    for fname, builder in (("root.schema.json", schema_gen.build_root_schema),
                           ("pipeline.schema.json", schema_gen.build_pipeline_schema)):
        on_disk = json.loads(_read(os.path.join(target, "schemas", fname)))
        assert on_disk == builder(), "scaffolded {} drifted from the generator".format(fname)


HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.normpath(os.path.join(HERE, "..", "examples", "hello-yaah"))


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def main() -> None:
    linear = load_template("linear")
    tmp = tempfile.mkdtemp(prefix="yaah-init-")
    try:
        target = os.path.join(tmp, "my-pipeline")
        n = scaffold(target)
        assert n == len(linear) + 2, (n, len(linear))  # +2 generated schema files

        # every declared file landed verbatim (config files gain a $schema
        # pointer); the generated schemas match the engine's own generator
        _assert_scaffolded_matches(target, linear)

        # the produced root validates — proves the embed isn't lying about shape,
        # AND that validate_root accepts the injected $schema pointer
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
        for rel, expected in linear.items():
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
    from yaah.init_template import load_template
    for name in ARCHETYPES:
        template = load_template(name)
        tmp = tempfile.mkdtemp(prefix="yaah-arch-{}-".format(name))
        try:
            target = os.path.join(tmp, "p")
            n = scaffold(target, name)
            assert n == len(template) + 2, (name, n, len(template))  # +2 schemas

            # every declared file landed (config files gain a $schema pointer);
            # the generated schemas match the engine's own generator
            _assert_scaffolded_matches(target, template)

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


def scenario_archetypes_and_descriptions_in_sync() -> None:
    """ARCHETYPES (the templates) and ARCHETYPE_DESCRIPTIONS (the catalog
    `yaah scaffold --list` prints) must declare the same keys. Without this
    check, adding a new template means `scaffold --list` shows "(no
    description)" — invisible drift the first user notices."""
    from yaah.init_template import ARCHETYPE_DESCRIPTIONS, ARCHETYPES
    missing = set(ARCHETYPES) - set(ARCHETYPE_DESCRIPTIONS)
    extra = set(ARCHETYPE_DESCRIPTIONS) - set(ARCHETYPES)
    assert not missing, "archetype(s) without a --list description: {}".format(missing)
    assert not extra, "description(s) for non-existent archetype(s): {}".format(extra)
    # The description text isn't empty / placeholder-only.
    for name, desc in ARCHETYPE_DESCRIPTIONS.items():
        assert len(desc) >= 30, "{}: description too short: {!r}".format(name, desc)
    print("PASS archetypes and --list descriptions are in sync")


def scenario_with_schema_ref_guards() -> None:
    """$schema injection is string-based (preserves formatting), so it must never
    produce invalid JSON: it injects as the first key of an object and leaves a
    non-object or empty `{}` untouched (a trailing comma would corrupt them)."""
    from yaah.init_template import _with_schema_ref
    out = _with_schema_ref('{\n  "a": 1\n}', "schemas/root.schema.json")
    assert json.loads(out) == {"$schema": "schemas/root.schema.json", "a": 1}, out
    assert out.startswith('{\n  "$schema":'), out  # first key, indent preserved
    assert _with_schema_ref("{}", "x") == "{}"      # empty object: untouched
    assert _with_schema_ref("[1, 2]", "x") == "[1, 2]"  # non-object: untouched
    # CRLF line endings: the indent capture must not swallow the \r -> still valid JSON
    crlf = _with_schema_ref('{\r\n  "a": 1\r\n}', "x")
    assert json.loads(crlf) == {"$schema": "x", "a": 1}, repr(crlf)
    # single-line object (no newline before the first key): falls back to 2-space indent
    one = _with_schema_ref('{"a": 1}', "x")
    assert json.loads(one) == {"$schema": "x", "a": 1}, repr(one)
    print("PASS _with_schema_ref injects safely and guards non/empty objects")


if __name__ == "__main__":
    main()
    scenario_scaffold_every_archetype()
    scenario_scaffold_unknown_archetype()
    scenario_archetypes_and_descriptions_in_sync()
    scenario_with_schema_ref_guards()
