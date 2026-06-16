"""--fake overlay — the single-root testability flag.

A root config carrying a `_fake` block: with `--fake`, that block's keys are
merged over the top level (typically replacing `providers` / `default_provider`
with a fake_scripted set). Lets one root file carry both real + testable shapes
so the dev doesn't maintain two near-duplicate root files. Asserts:

  1. Without `--fake`, `_apply_fake_overlay` is a no-op (root passes through).
  2. With `--fake`, top-level keys named in `_fake` are replaced.
  3. Top-level keys NOT named in `_fake` survive unchanged.
  4. A non-dict `_fake` raises (loud failure at boundary).

Run: cd yaah && PYTHONPATH=src python3 tests/test_fake_overlay.py
"""
from __future__ import annotations

from yaah.runtime import _apply_fake_overlay


def test_no_fake_block_is_identity() -> None:
    root = {"providers": {"claude": {"type": "claude_cli"}}, "default_provider": "claude"}
    out = _apply_fake_overlay(dict(root))  # copy so we can compare
    assert out == root, "no _fake block must round-trip"


def test_fake_replaces_named_keys() -> None:
    root = {
        "providers": {"claude": {"type": "claude_cli"}},
        "default_provider": "claude",
        "state": {"type": "memory"},
        "_fake": {
            "providers": {"claude": {"type": "fake_scripted", "outputs": {"t": "ok"}}},
            "default_provider": "claude",
        },
    }
    out = _apply_fake_overlay(root)
    assert out["providers"]["claude"]["type"] == "fake_scripted", "provider type swapped"
    assert out["providers"]["claude"]["outputs"] == {"t": "ok"}, "fake outputs land"
    assert out["state"] == {"type": "memory"}, "unrelated keys survive"
    assert "_fake" not in out, "_fake block is consumed"


def test_non_dict_fake_raises() -> None:
    try:
        _apply_fake_overlay({"_fake": ["not", "a", "dict"]})
    except ValueError as e:
        assert "_fake" in str(e), "error names the bad key"
        return
    raise AssertionError("non-dict _fake should raise ValueError")


def main() -> None:
    test_no_fake_block_is_identity()
    test_fake_replaces_named_keys()
    test_non_dict_fake_raises()
    print("test_fake_overlay: PASS (3 scenarios)")


if __name__ == "__main__":
    main()
