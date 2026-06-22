"""yaah.doctor: diagnostic checks for `yaah doctor`.

What it proves: every check returns the shape diagnose() promises (exit_code,
text); the packaged base configs all resolve; the optional-dep check tolerates
both presence and absence (it never raises); the report contains the dimensions
an operator expects (Python, version, deps, bases, summary).

Run: cd yaah && PYTHONPATH=src python3 tests/test_doctor.py
"""
from __future__ import annotations

from yaah.doctor import (
    _check_optional_deps,
    _check_packaged_bases,
    _check_python_version,
    _check_yaah_version,
    diagnose,
)


def scenario_python_version() -> None:
    ok, label = _check_python_version()
    assert ok is True, label                  # we ship and run on 3.9+
    assert label.startswith("Python "), label


def scenario_version_string_is_resolvable() -> None:
    v = _check_yaah_version()
    assert isinstance(v, str) and v, v       # either installed version or fallback


def scenario_optional_deps_never_raise() -> None:
    """The check returns one row per declared extras even when nothing is
    installed — absence is a feature, not an error."""
    rows = _check_optional_deps()
    # The 4 extras declared in pyproject.toml: litellm, nats, langfuse, http.
    names = {row[1] for row in rows}
    assert {"litellm", "nats", "langfuse", "httpx"}.issubset(names), names
    for extras, modname, ok, purpose in rows:
        assert isinstance(ok, bool), (modname, ok)
        assert purpose, (modname, "purpose must be non-empty")


def scenario_packaged_bases_resolve() -> None:
    """The three base configs the engine ships must all parse — if one fails
    here, the wheel was built without package-data."""
    rows = _check_packaged_bases()
    assert len(rows) == 3, rows
    for rel, ok, msg in rows:
        assert ok is True, (rel, msg)


def scenario_diagnose_report_shape() -> None:
    """Smoke: the full report mentions every dimension and ends with a
    DOCTOR: ok summary line (assuming packaged bases resolve and we're on a
    supported Python — both checked individually above)."""
    code, report = diagnose()
    assert code == 0, (code, report)
    assert "yaah " in report                                # version line
    assert "Python " in report                              # Python check
    assert "optional dependencies:" in report
    assert "packaged base configs" in report
    assert report.rstrip().endswith("DOCTOR: ok"), report


def main() -> None:
    scenario_python_version()
    scenario_version_string_is_resolvable()
    scenario_optional_deps_never_raise()
    scenario_packaged_bases_resolve()
    scenario_diagnose_report_shape()
    print("ok")


if __name__ == "__main__":
    main()
