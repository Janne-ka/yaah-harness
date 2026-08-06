"""build_catalog's hand-written fence contract — the one thing a regen must NOT eat.

What it proves: a block between `<!-- BEGIN-HANDWRITTEN: key -->` and its END
marker survives `regenerate()` byte-for-byte, markers included; several blocks
keep their relative order; prose OUTSIDE a fence is (by design) overwritten; a
malformed fence raises instead of silently dropping human prose; and the real
docs/module-catalog.md's `terminology` block round-trips.

Run: cd yaah && PYTHONPATH=src python3 tests/test_catalog_handwritten.py
"""
from __future__ import annotations

import importlib.util
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "build_catalog.py")

_spec = importlib.util.spec_from_file_location("build_catalog", SCRIPT)
build_catalog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_catalog)

TERMINOLOGY = "\n".join([
    "<!-- BEGIN-HANDWRITTEN: terminology -->",
    "## Terminology",
    "",
    "| Term | Expansion |",
    "|---|---|",
    "| `corr` | `Envelope.correlation_id` |",
    "<!-- END-HANDWRITTEN: terminology -->",
])

NOTES = "\n".join([
    "<!-- BEGIN-HANDWRITTEN: notes -->",
    "## Notes",
    "",
    "  indented   text	with a tab, and a bare <!-- comment --> inside.",
    "<!-- END-HANDWRITTEN: notes -->",
])


def _raises(fn, needle: str) -> None:
    try:
        fn()
    except ValueError as e:
        assert needle in str(e), (needle, str(e))
        return
    raise AssertionError("expected ValueError containing " + needle)


def scenario_block_survives_regen_verbatim() -> None:
    existing = "# stale heading\n\nsome overwritten prose\n\n" + TERMINOLOGY + "\n"
    out = build_catalog.regenerate(existing)
    assert TERMINOLOGY in out, out[-800:]
    assert "some overwritten prose" not in out       # outside the fence = generated space
    assert out.startswith("# YAAH module catalog")


def scenario_several_blocks_keep_their_order() -> None:
    existing = TERMINOLOGY + "\n\nfiller\n\n" + NOTES + "\n"
    out = build_catalog.regenerate(existing)
    assert out.index(TERMINOLOGY) < out.index(NOTES), out[-800:]


def scenario_extract_is_verbatim_and_ordered() -> None:
    blocks = build_catalog.extract_handwritten(NOTES + "\n\n" + TERMINOLOGY + "\n")
    assert blocks == [NOTES, TERMINOLOGY], blocks


def scenario_no_fence_means_no_carry_over() -> None:
    assert build_catalog.extract_handwritten("# doc\n\n## Terminology\n") == []


def scenario_malformed_fences_raise() -> None:
    _raises(lambda: build_catalog.extract_handwritten(
        "<!-- BEGIN-HANDWRITTEN: a -->\nbody\n"), "unterminated")
    _raises(lambda: build_catalog.extract_handwritten(
        "<!-- BEGIN-HANDWRITTEN: a -->\n<!-- END-HANDWRITTEN: b -->\n"), "closes block a")
    _raises(lambda: build_catalog.extract_handwritten(
        TERMINOLOGY + "\n" + TERMINOLOGY + "\n"), "duplicate handwritten key")
    _raises(lambda: build_catalog.extract_handwritten(
        "<!-- END-HANDWRITTEN: a -->\n"), "no BEGIN")


def scenario_real_catalog_terminology_round_trips() -> None:
    existing = build_catalog.OUT_MD.read_text()
    blocks = build_catalog.extract_handwritten(existing)
    assert any("## Terminology" in b for b in blocks), blocks
    out = build_catalog.regenerate(existing)
    for b in blocks:
        assert b in out, b[:200]


def main() -> None:
    scenario_block_survives_regen_verbatim()
    scenario_several_blocks_keep_their_order()
    scenario_extract_is_verbatim_and_ordered()
    scenario_no_fence_means_no_carry_over()
    scenario_malformed_fences_raise()
    scenario_real_catalog_terminology_round_trips()
    print("ok")


if __name__ == "__main__":
    main()
