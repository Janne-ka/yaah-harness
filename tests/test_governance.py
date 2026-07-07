"""Governance checks — whole-tree core purity and domain-leakage.

Proves that (a) the shared check functions catch violations when given hostile
content and (b) the current engine source tree passes both guards end-to-end.
These run on every `scripts/run_tests.py` invocation so CI defends the
zero-dep + domain-free edges continuously, not just on changed lines.

Run: cd yaah && PYTHONPATH=src python3 tests/test_governance.py
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
rpr = importlib.import_module("review_my_pr")


# ---------- helpers ----------------------------------------------------------

def _fixed_lines(content: str):
    """Return a get_lines callable that ignores the path and returns fixed content."""
    lines = content.splitlines()
    return lambda _p: lines


# ---------- core purity — falsifying (adversarial) --------------------------

def catches_third_party_import_in_core() -> None:
    """Checker must FAIL when a core file imports a third-party library."""
    fake = Path("src/yaah/core/bad_module.py")
    v, msg = rpr.check_core_purity([fake], get_lines=_fixed_lines("import numpy\n"))
    assert v == "FAIL", f"expected FAIL, got {v!r}: {msg}"
    assert "numpy" in msg


def catches_third_party_import_in_harness() -> None:
    """Checker must FAIL for harness/ as well, not just core/."""
    fake = Path("src/yaah/harness/bad_module.py")
    v, msg = rpr.check_core_purity(
        [fake], get_lines=_fixed_lines("from litellm import completion\n")
    )
    assert v == "FAIL", f"expected FAIL, got {v!r}: {msg}"
    assert "litellm" in msg


def passes_stdlib_only_in_core() -> None:
    """stdlib imports in zero-dep core are allowed."""
    fake = Path("src/yaah/core/clean.py")
    v, _msg = rpr.check_core_purity(
        [fake], get_lines=_fixed_lines("import asyncio\nfrom typing import List\n")
    )
    assert v == "PASS", f"expected PASS, got {v!r}"


def passes_yaah_import_in_core() -> None:
    """Intra-package yaah.* imports in core are allowed."""
    fake = Path("src/yaah/core/clean.py")
    v, _msg = rpr.check_core_purity(
        [fake], get_lines=_fixed_lines("from yaah.core import Envelope\n")
    )
    assert v == "PASS", f"expected PASS, got {v!r}"


def ignores_non_core_file_for_purity() -> None:
    """Adapter files outside CORE_ROOTS are not subject to zero-dep constraint."""
    fake = Path("src/yaah/adapters/providers/some_provider.py")
    v, _msg = rpr.check_core_purity(
        [fake], get_lines=_fixed_lines("import litellm\n")
    )
    assert v == "PASS", f"expected PASS for non-core file, got {v!r}"


# ---------- domain leakage — falsifying (adversarial) -----------------------

def catches_banlist_term_in_engine() -> None:
    """Checker must FAIL when engine code contains a banlist term."""
    words = rpr.load_banlist()
    if not words:
        # banlist files absent in this checkout — acceptable in a stripped CI
        # environment; skip rather than false-pass.
        print("  NOTE: banlist empty — catches_banlist_term_in_engine skipped")
        return
    term = words[0]
    fake = Path("src/yaah/fake_node.py")
    v, msg = rpr.check_domain_leakage([fake], get_lines=_fixed_lines(f"# {term}\n"))
    assert v == "FAIL", f"expected FAIL for term '{term}', got {v!r}: {msg}"
    assert term.lower() in msg.lower(), f"expected term '{term}' in message: {msg}"


def passes_clean_engine_content_domain() -> None:
    """Clean engine content must not trip domain leakage."""
    fake = Path("src/yaah/clean_node.py")
    v, _msg = rpr.check_domain_leakage(
        [fake], get_lines=_fixed_lines("import asyncio\n")
    )
    assert v == "PASS", f"expected PASS, got {v!r}"


def ignores_non_engine_file_for_domain() -> None:
    """Files outside ENGINE_ROOT (e.g. examples/) are not checked for banlist terms."""
    words = rpr.load_banlist()
    if not words:
        return
    term = words[0]
    fake = Path("examples/my_pipeline.py")
    v, _msg = rpr.check_domain_leakage(
        [fake], get_lines=_fixed_lines(f"# {term}\n")
    )
    assert v == "PASS", f"expected PASS for non-engine file, got {v!r}"


# ---------- whole-tree: actual engine tree must pass both guards -------------

def whole_tree_core_purity_passes() -> None:
    """The live engine tree must have zero third-party imports in the zero-dep core."""
    files = rpr.engine_src_files()
    assert files, "engine_src_files() returned empty list — path configuration error"
    v, msg = rpr.check_core_purity(files, get_lines=rpr.all_lines_for)
    assert v == "PASS", f"whole-tree core purity FAIL — offenders:\n{msg}"


def whole_tree_domain_leakage_passes() -> None:
    """The live engine tree must contain no banlist terms."""
    files = rpr.engine_src_files()
    v, msg = rpr.check_domain_leakage(files, get_lines=rpr.all_lines_for)
    assert v == "PASS", f"whole-tree domain leakage FAIL — offenders:\n{msg}"


# ---------- driver -----------------------------------------------------------

def main() -> None:
    catches_third_party_import_in_core()
    catches_third_party_import_in_harness()
    passes_stdlib_only_in_core()
    passes_yaah_import_in_core()
    ignores_non_core_file_for_purity()
    catches_banlist_term_in_engine()
    passes_clean_engine_content_domain()
    ignores_non_engine_file_for_domain()
    whole_tree_core_purity_passes()
    whole_tree_domain_leakage_passes()
    print("ok")


if __name__ == "__main__":
    main()
