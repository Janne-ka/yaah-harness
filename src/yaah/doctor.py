"""doctor — diagnostic checks for `yaah doctor`.

Used by: the CLI `doctor` subcommand. Answers the "did I install this right?"
moment between `pip install yaah-harness` and the first `yaah run` — without
trying to actually run a pipeline.
Where: a leaf module, no engine state. PURE checks (no I/O beyond reading
packaged resources via importlib), so it's safe to call from any environment
including CI / test fixtures.
Why: the failure mode without it is "yaah run x.json → ModuleNotFoundError:
litellm" five minutes after install — the user blames yaah. Doctor surfaces
the dependency state up front and answers in one screen.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
import sys
from typing import List, Tuple


# Optional adapters declared in pyproject.toml. Each is (extras-name,
# import-name, what-it-unlocks) — the third column is the user-facing
# "why does this matter" so the report reads as guidance, not a stack trace.
_OPTIONAL_DEPS = [
    ("litellm",  "litellm",  "LiteLLMBackend (provider:model via litellm)"),
    ("nats",     "nats",     "NatsComms transport (distributed runs)"),
    ("langfuse", "langfuse", "Langfuse trace sink"),
    ("http",     "httpx",    "HttpPromptSource default fetcher"),
]

# Packaged base configs the engine relies on for `_extends: "yaah:bases/..."`.
# If these don't resolve, `pip install` missed package-data and every scaffolded
# pipeline that references a base will fail at load.
_PACKAGED_BASES = [
    "bases/local.base.json",
    "bases/nats.base.json",
    "bases/trace-audit.base.json",
]


def _check_python_version() -> Tuple[bool, str]:
    """3.9 is the project's documented minimum (pyproject + tests file headers).
    Below that, downstream imports start failing in non-obvious ways."""
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 9)
    return ok, "Python {}.{}.{}".format(v.major, v.minor, v.micro)


def _check_yaah_version() -> str:
    """Defer to cli._resolve_version so doctor and `--version` agree."""
    from .cli import _resolve_version
    return _resolve_version()


def _check_optional_deps() -> List[Tuple[str, str, bool, str]]:
    """Return one row per optional dep: (extras, import, importable, purpose).
    Optional deps are never errors — they're features the user can opt into;
    doctor reports them as info so the operator knows what `pip install
    'yaah-harness[litellm]'` would unlock."""
    out: List[Tuple[str, str, bool, str]] = []
    for extras, modname, purpose in _OPTIONAL_DEPS:
        try:
            __import__(modname)
            ok = True
        except ImportError:
            ok = False
        out.append((extras, modname, ok, purpose))
    return out


def _check_packaged_bases() -> List[Tuple[str, bool, str]]:
    """Read each shipped base config via importlib.resources — same path the
    runtime takes for `_extends: "yaah:bases/..."`. If a base doesn't resolve
    or isn't valid JSON, that's a real install problem (package-data missing
    from the wheel); doctor names which one."""
    import importlib.resources as ir
    out: List[Tuple[str, bool, str]] = []
    trav = ir.files("yaah.configs")
    for rel in _PACKAGED_BASES:
        node = trav
        for part in rel.split("/"):
            node = node.joinpath(part)
        try:
            text = node.read_text(encoding="utf-8")
            json.loads(text)        # parse — catches package-data truncation too
            out.append((rel, True, "ok"))
        except (FileNotFoundError, OSError) as e:
            out.append((rel, False, "missing ({})".format(e.__class__.__name__)))
        except json.JSONDecodeError as e:
            out.append((rel, False, "invalid JSON — {}".format(e.msg)))
    return out


def _glyph(ok: bool) -> str:
    return "✓" if ok else "✗"


def diagnose() -> Tuple[int, str]:
    """Run every check, return (exit_code, report_text). PURE — no print, no
    side effects beyond imports + reading packaged resources. The CLI wraps it
    so tests can assert the report shape without capturing stdout."""
    py_ok, py_str = _check_python_version()
    version = _check_yaah_version()
    deps = _check_optional_deps()
    bases = _check_packaged_bases()

    lines: List[str] = []
    lines.append("yaah {}".format(version))
    lines.append("")
    lines.append("{} {} (minimum 3.9)".format(_glyph(py_ok), py_str))
    lines.append("")
    lines.append("optional dependencies:")
    for extras, modname, ok, purpose in deps:
        # Always show the row so the operator sees the full menu of what each
        # extras-key unlocks — even when a dep is absent, the line tells them
        # what they're missing and how to opt in.
        if ok:
            lines.append("  {} {} — {}".format(_glyph(True), modname, purpose))
        else:
            lines.append("  {} {} — {} (pip install 'yaah-harness[{}]')".format(
                _glyph(False), modname, purpose, extras))
    lines.append("")
    lines.append("packaged base configs (yaah:bases/...):")
    for rel, ok, msg in bases:
        lines.append("  {} {} — {}".format(_glyph(ok), rel, msg))

    # Exit non-zero ONLY when something we promised to ship isn't shippable.
    # Optional deps absent => fine (operator just hasn't installed extras).
    # Packaged bases missing => the wheel was built wrong, doctor must fail.
    hard_failures = (not py_ok) or any(not ok for _, ok, _ in bases)
    exit_code = 1 if hard_failures else 0

    lines.append("")
    if hard_failures:
        lines.append("DOCTOR: 1 or more hard checks failed (see ✗ above)")
    else:
        lines.append("DOCTOR: ok")
    return exit_code, "\n".join(lines) + "\n"
