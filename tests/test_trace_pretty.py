"""trace.pretty: per-run tree rendering for `yaah trace --pretty`.

Run: cd yaah && PYTHONPATH=src python3 tests/test_trace_pretty.py
"""
from __future__ import annotations

from yaah.trace.pretty import errors_only, pretty


def _records():
    # Two runs, with stages + a model_call child + an error on the second run.
    # Matches the shape PhaseContributor + CostContributor emit (see record.py).
    return [
        # --- run abc ---
        {"id": "s1", "corr": "abc", "name": "stage", "parent": "p0",
         "duration_ms": 245.0, "status": "ok", "stage": "draft"},
        {"id": "m1", "corr": "abc", "name": "model_call", "parent": "s1",
         "duration_ms": 230.0, "tokens_in": 1200, "tokens_out": 340,
         "model": "claude:sonnet", "status": "ok"},
        {"id": "s2", "corr": "abc", "name": "stage", "parent": "p0",
         "duration_ms": 12.0, "status": "ok", "stage": "validate"},
        # --- run def ---
        {"id": "s3", "corr": "def", "name": "stage", "parent": "p1",
         "duration_ms": 400.0, "status": "error", "stage": "verify",
         "error": "json_object validator failed — missing 'summary'"},
        {"id": "s4", "corr": "def", "name": "stage", "parent": "p1",
         "duration_ms": 300.0, "status": "ok", "stage": "verify"},
    ]


def scenario_smoke() -> None:
    """The pretty output covers the runs, stages, calls, and the error rollup —
    one assertion per dimension so a regression names what broke."""
    out = pretty(_records())
    # Header rollup
    assert "2 runs" in out and "4 stages" in out and "1 model call" in out
    assert "1 error" in out
    # Per-run header
    assert "run abc" in out and "run def" in out
    # Stage tree under run abc
    assert 'stage "draft"' in out and "245ms" in out
    assert "model_call" in out and "claude:sonnet" in out
    assert "1.2k→340 tokens" in out
    # Status glyphs (ok ✓, error ✗)
    assert "✓" in out and "✗" in out
    # Errors section names run + stage + detail
    assert "errors:" in out
    assert 'run def stage "verify": json_object validator failed' in out


def scenario_empty() -> None:
    """No records -> a clear placeholder, not a stack trace."""
    assert pretty([]).strip() == "(no records)"


def scenario_cost_via_price_map() -> None:
    """Token costs flow through aggregate.cost_usd via the price-map — the
    pretty view shows dollars when prices are provided, omits them when not."""
    records = [
        {"id": "s1", "corr": "r1", "name": "stage", "parent": "p",
         "duration_ms": 100.0, "status": "ok", "stage": "draft"},
        {"id": "m1", "corr": "r1", "name": "model_call", "parent": "s1",
         "duration_ms": 95.0, "tokens_in": 1000, "tokens_out": 500,
         "model": "claude:sonnet"},
    ]
    price_map = {"claude:sonnet": {"input": 3.0, "output": 15.0}}  # $/1k tokens
    out = pretty(records, price_map=price_map)
    # 1000/1000 * 3.00 + 500/1000 * 15.00 = $10.50 — shown at $-precision 3
    assert "$10.500" in out
    # without prices: no $ marker on either header or call line
    assert "$" not in pretty(records)


def scenario_tool_call_render() -> None:
    """tool_call children nest under their parent stage with the tool name."""
    records = [
        {"id": "s1", "corr": "r1", "name": "stage", "parent": "p",
         "duration_ms": 200.0, "status": "ok", "stage": "act"},
        {"id": "t1", "corr": "r1", "name": "tool_call", "parent": "s1",
         "duration_ms": 50.0, "tool": "read_file"},
    ]
    out = pretty(records)
    assert 'stage "act"' in out
    assert "tool_call" in out and "read_file" in out
    assert "1 tool" in out  # per-run header mentions tools when present


def scenario_suspended_status() -> None:
    """A parked human gate stage renders with the pause glyph (⏸), not error."""
    records = [
        {"id": "s1", "corr": "r1", "name": "stage", "parent": "p",
         "duration_ms": 5.0, "status": "suspended", "stage": "gate"},
    ]
    out = pretty(records)
    assert "⏸" in out and "✗" not in out


def scenario_errors_only_clean_exits_zero() -> None:
    """A trace with no error spans -> exit 0, friendly 'no errors' message.
    This is the CI happy-path; the operator runs `yaah trace x --errors-only`
    in a pre-commit hook and gets exit 0 + silence-equivalent on a clean run."""
    clean = [
        {"id": "s1", "corr": "r1", "name": "stage", "parent": "p",
         "duration_ms": 10.0, "status": "ok", "stage": "draft"},
    ]
    code, msg = errors_only(clean)
    assert code == 0, (code, msg)
    assert msg.strip() == "no errors", msg


def scenario_errors_only_with_errors_exits_one() -> None:
    """A trace with error-status spans -> exit 1, one line per error naming
    run + stage + detail. The error rollup is the only thing printed — no
    tree, no headers, just the bad news."""
    dirty = _records()                            # reuses the smoke fixture (1 error)
    code, msg = errors_only(dirty)
    assert code == 1, (code, msg)
    assert 'run def stage "verify"' in msg, msg   # names run + stage
    assert "json_object validator failed" in msg, msg  # detail preserved
    # No tree headers leak in
    assert "├─" not in msg and "stages" not in msg, msg


def main() -> None:
    scenario_smoke()
    scenario_empty()
    scenario_cost_via_price_map()
    scenario_tool_call_render()
    scenario_suspended_status()
    scenario_errors_only_clean_exits_zero()
    scenario_errors_only_with_errors_exits_one()
    print("ok")


if __name__ == "__main__":
    main()
