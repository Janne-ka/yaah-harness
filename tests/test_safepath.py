"""safe_join: containment for file-adapter key resolution.

Used by FileDataSource / FileSink / FilePromptSource / FileMcpSource to keep a
relative key from escaping its base. Absolute keys still pass through (operator
intent, trusted-config model). See yaah/safepath.py for the contract.

Run: cd yaah && PYTHONPATH=src python3 tests/test_safepath.py
"""
from __future__ import annotations

import os
import tempfile

from yaah.safepath import safe_join


def scenario_relative_key_returns_joined_path() -> None:
    # contract: return the joined path callers expect (NOT realpath-canonicalized),
    # so downstream open()/asserts see what they passed in.
    with tempfile.TemporaryDirectory() as base:
        os.makedirs(os.path.join(base, "sub"))
        assert safe_join(base, "ok.txt") == os.path.join(base, "ok.txt")
        assert safe_join(base, "sub/inner.txt") == os.path.join(base, "sub/inner.txt")


def scenario_relative_escape_raises() -> None:
    with tempfile.TemporaryDirectory() as base:
        for bad in ["../escape", "../../escape", "sub/../../escape"]:
            try:
                safe_join(base, bad)
            except ValueError:
                continue
            raise AssertionError("expected escape rejection for {!r}".format(bad))


def scenario_absolute_passes_through() -> None:
    # absolute keys are operator intent — returned as given
    with tempfile.TemporaryDirectory() as base:
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            abs_path = tf.name
        try:
            assert safe_join(base, abs_path) == abs_path
        finally:
            os.unlink(abs_path)


def scenario_disallow_absolute_blocks_passthrough() -> None:
    # the option an HTTP-exposed sink would use
    with tempfile.TemporaryDirectory() as base:
        try:
            safe_join(base, "/etc/passwd", allow_absolute=False)
        except ValueError:
            return
        raise AssertionError("expected absolute-rejection with allow_absolute=False")


def scenario_no_base_no_containment() -> None:
    # cwd-relative legacy mode — pass through unchanged
    assert safe_join("", "any/relative/path.txt") == "any/relative/path.txt"
    assert safe_join(None, "any/relative/path.txt") == "any/relative/path.txt"


def scenario_symlink_pointing_outside_is_blocked() -> None:
    # the realpath-based check catches a symlink whose target leaves base —
    # the surface attacks an attacker who controls the file tree under base.
    with tempfile.TemporaryDirectory() as base:
        with tempfile.TemporaryDirectory() as outside:
            link = os.path.join(base, "trap")
            os.symlink(outside, link)
            try:
                safe_join(base, "trap/file.txt")
            except ValueError:
                return
            raise AssertionError("expected symlink-target escape rejection")


def main() -> None:
    scenario_relative_key_returns_joined_path()
    scenario_relative_escape_raises()
    scenario_absolute_passes_through()
    scenario_disallow_absolute_blocks_passthrough()
    scenario_no_base_no_containment()
    scenario_symlink_pointing_outside_is_blocked()
    print("ok")


if __name__ == "__main__":
    main()
