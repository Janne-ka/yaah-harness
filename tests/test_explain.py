"""R13 — `yaah <root> --explain` prints the EFFECTIVE root config (post-`_extends`
+ post-`_fake` + defaults) with per-key provenance: `(user)`, `(extends:<base>)`,
`(fake)`, or `(default)`. Spring `--debug` conditions report / `helm template` /
`terraform plan` for yaah: 'what would actually load right now?'

Run: cd yaah && PYTHONPATH=src python3 tests/test_explain.py
"""
from __future__ import annotations

import io
import json
import os
import tempfile
from contextlib import redirect_stdout
from typing import Any, Dict

from yaah.runtime import explain_root


def _write(d: str, name: str, obj: Dict[str, Any]) -> str:
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    return p


def _explain(root_path: str, *, fake: bool = False) -> str:
    """Run explain_root on a file path, capture its stdout."""
    from yaah.runtime_factories import _read_json
    from yaah.runtime import _apply_fake_overlay
    with open(root_path, "r", encoding="utf-8") as f:
        raw_user = json.load(f)
    expanded = _read_json(root_path)
    if fake:
        expanded = _apply_fake_overlay(expanded)
    base = os.path.dirname(os.path.abspath(root_path))
    buf = io.StringIO()
    with redirect_stdout(buf):
        explain_root(raw_user, expanded, base, root_path=root_path, fake=fake)
    return buf.getvalue()


def scenario_user_key_marked_user() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = _write(d, "r.json", {
            "transport": {"type": "inproc"},
            "providers": {"claude": {"type": "claude_cli"}},
            "default_provider": "claude",
            "prompt_sources": {"file": {"type": "file", "dir": "prompts"}},
            "default_prompt_source": "file",
            "pipeline": "p.json",
        })
        out = _explain(root)
        assert "transport" in out and "(user)" in out, out
        # the EFFECTIVE JSON should be printed too
        assert '"type": "inproc"' in out, out


def scenario_default_key_marked_default() -> None:
    """An OMITTED `state` defaults to memory — provenance should show that."""
    with tempfile.TemporaryDirectory() as d:
        root = _write(d, "r.json", {
            "transport": {"type": "inproc"},
            "providers": {"claude": {"type": "claude_cli"}},
            "default_provider": "claude",
            "prompt_sources": {"file": {"type": "file", "dir": "prompts"}},
            "default_prompt_source": "file",
            "pipeline": "p.json",
        })
        out = _explain(root)
        # state is absent in the user file; its default should surface
        assert "state" in out and "(default)" in out, out
        assert "memory" in out, out


def scenario_extends_key_marked_extends() -> None:
    """A key contributed by `_extends` base (not in the user file) should show its
    source base file name."""
    with tempfile.TemporaryDirectory() as d:
        _write(d, "base.json", {
            "transport": {"type": "inproc"},
            "state": {"type": "memory"},
        })
        root = _write(d, "r.json", {
            "_extends": "base.json",
            "providers": {"claude": {"type": "claude_cli"}},
            "default_provider": "claude",
            "prompt_sources": {"file": {"type": "file", "dir": "prompts"}},
            "default_prompt_source": "file",
            "pipeline": "p.json",
        })
        out = _explain(root)
        assert "transport" in out and "(extends:base.json)" in out, out
        # the EFFECTIVE config should include the inherited transport
        assert '"type": "inproc"' in out, out


def scenario_fake_overlay_marked_fake() -> None:
    """`--fake` overlay should attribute its keys to `(fake)`."""
    with tempfile.TemporaryDirectory() as d:
        root = _write(d, "r.json", {
            "transport": {"type": "inproc"},
            "providers": {"claude": {"type": "claude_cli"}},
            "default_provider": "claude",
            "prompt_sources": {"file": {"type": "file", "dir": "prompts"}},
            "default_prompt_source": "file",
            "pipeline": "p.json",
            "_fake": {"providers": {"fake": {"type": "fake_scripted",
                                              "fixtures": "f.json"}},
                      "default_provider": "fake"},
        })
        out = _explain(root, fake=True)
        # both `providers` and `default_provider` came from the fake block
        assert "providers" in out and "(fake)" in out, out
        # the effective value should be the fake one
        assert "fake_scripted" in out, out


def scenario_underscore_keys_excluded_from_report() -> None:
    """`_fake`, `_about` etc. are comments — they should NEVER appear in the
    explain report (whether `--fake` is on or off)."""
    with tempfile.TemporaryDirectory() as d:
        root = _write(d, "r.json", {
            "transport": {"type": "inproc"},
            "providers": {"claude": {"type": "claude_cli"}},
            "default_provider": "claude",
            "prompt_sources": {"file": {"type": "file", "dir": "prompts"}},
            "default_prompt_source": "file",
            "pipeline": "p.json",
            "_about": "doc string",
            "_fake": {"providers": {"f": {"type": "fake_scripted", "fixtures": "f.json"}}},
        })
        # Provenance line for a key looks like "  <key>  (source)  <value>" —
        # so the regression we're guarding is "an underscore key gets a row."
        def _has_row(report: str, key: str) -> bool:
            return any(line.startswith("  " + key + " ") or line.startswith("  " + key + "  ")
                       for line in report.splitlines())
        out_no_fake = _explain(root, fake=False)
        assert not _has_row(out_no_fake, "_fake"), out_no_fake
        assert not _has_row(out_no_fake, "_about"), out_no_fake
        # also: no underscore key in the dumped JSON keys
        assert '"_fake":' not in out_no_fake and '"_about":' not in out_no_fake, out_no_fake
        out_fake = _explain(root, fake=True)
        assert not _has_row(out_fake, "_fake") and not _has_row(out_fake, "_about"), out_fake
        assert '"_fake":' not in out_fake and '"_about":' not in out_fake, out_fake


def scenario_validation_errors_surface_before_print() -> None:
    """An invalid root must fail loud (with the R15 messages) instead of printing
    a confused effective config."""
    with tempfile.TemporaryDirectory() as d:
        root = _write(d, "r.json", {
            "transport": "inproc",  # bare-string trap
            "pipeline": "p.json",
        })
        try:
            _explain(root)
        except ValueError as e:
            msg = str(e)
            assert "transport" in msg and "typed-block" in msg, msg
            return
        raise AssertionError("invalid root should raise from explain")


def main() -> None:
    scenario_user_key_marked_user()
    scenario_default_key_marked_default()
    scenario_extends_key_marked_extends()
    scenario_fake_overlay_marked_fake()
    scenario_underscore_keys_excluded_from_report()
    scenario_validation_errors_surface_before_print()
    print("test_explain: PASS (6 scenarios)")


if __name__ == "__main__":
    main()
