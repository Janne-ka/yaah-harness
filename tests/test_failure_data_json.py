"""Structured failure carriage: Failure gains an optional `data` dict so a code
can carry machine fields, and StageFailed exposes the whole verdict as a JSON
object (the shape run/resume --json and the MCP error content print).

Covers:
  - Failure.data defaults empty and is additive (old 3-arg constructors unchanged);
  - it survives the Verdict <-> Envelope round-trip (to_envelope / from_envelope);
  - render's render_unfilled_placeholders puts the unfilled key LIST in data;
  - StageFailed.to_failure_json() -> {"outcome":"failed","stage","failures":[...]}
    with each failure carrying code/message/fix_hint (+ data when non-empty).

Run: cd yaah && PYTHONPATH=src python3 tests/test_failure_data_json.py
"""
from __future__ import annotations

import asyncio

from yaah.core import Envelope, Failure, Kind, NodeConfig, Verdict
from yaah.harness import Cleared, Done, StageFailed, Suspended
from yaah.nodes.render_node import RenderNode


def test_failure_data_additive_default():
    # existing positional/keyword constructors keep working; data defaults empty
    f = Failure("boom", "it broke", "fix it")
    assert f.code == "boom" and f.fix_hint == "fix it"
    assert f.data == {}, f.data
    f2 = Failure("boom", "msg")            # 2-arg still valid
    assert f2.fix_hint is None and f2.data == {}


def test_failure_data_survives_envelope_roundtrip():
    v = Verdict.failed(Failure("c", "m", "h", data={"keys": ["title", "date"]}))
    env = v.to_envelope()
    back = Verdict.from_envelope(env)
    assert back.failures[0].data == {"keys": ["title", "date"]}, back.failures[0]
    # and the serialized payload carries it as plain JSON data
    assert env.payload["failures"][0]["data"] == {"keys": ["title", "date"]}, env.payload


def test_render_unfilled_puts_key_list_in_data():
    node = RenderNode(template="Report: {{title}} on {{date}}")
    out = asyncio.run(node.invoke(Envelope("task", {}), NodeConfig()))
    assert out.kind == Kind.VERDICT, out
    v = Verdict.from_envelope(out)
    assert v.failures[0].code == "render_unfilled_placeholders", v.failures
    assert v.failures[0].data.get("unfilled") == ["title", "date"], v.failures[0].data


def test_stage_failed_to_failure_json():
    v = Verdict.failed(Failure("render_unfilled_placeholders", "no value for: title",
                               "add a parse step", data={"unfilled": ["title"]}))
    sf = StageFailed("render", v)
    obj = sf.to_failure_json()
    assert obj["outcome"] == "failed", obj
    assert obj["stage"] == "render", obj
    f = obj["failures"][0]
    assert f["code"] == "render_unfilled_placeholders", obj
    assert f["message"] and f["fix_hint"], obj
    assert f["data"] == {"unfilled": ["title"]}, obj
    # a failure with NO data omits the key (stays minimal)
    plain = StageFailed("do", Verdict.failed(Failure("x", "m"))).to_failure_json()
    assert "data" not in plain["failures"][0], plain


def test_outcome_to_json_dict_single_source():
    # Each Outcome owns its --json shape via to_json_dict; both the CLI and the
    # MCP handlers dispatch to it, so the two surfaces cannot drift.
    done = Done(output=Envelope("done", {"raw": "ok"}), baton_id="b1")
    assert done.to_json_dict() == {
        "outcome": "done", "baton_id": "b1", "payload": {"raw": "ok"}}

    susp = Suspended(baton_id="b2", awaiting="spec:approve",
                     concerns=[{"code": "c", "message": "m"}], ask="Approve?")
    d = susp.to_json_dict()
    assert d == {"outcome": "suspended", "baton_id": "b2",
                 "awaiting": "spec:approve",
                 "concerns": [{"code": "c", "message": "m"}], "ask": "Approve?"}, d
    # concerns are copied, not aliased — mutating the result can't touch the Outcome
    d["concerns"][0]["code"] = "X"
    assert susp.concerns[0]["code"] == "c", susp.concerns

    cl = Cleared(baton_id="b3", node="do", payload={"who": "op"})
    assert cl.to_json_dict() == {
        "outcome": "cleared", "baton_id": "b3", "node": "do",
        "payload": {"who": "op"}}


def test_cli_and_mcp_outcome_json_agree():
    # both surfaces' _outcome_json dispatch to the same to_json_dict
    from yaah.adapters.mcp_server.tools import _outcome_json as mcp_json
    from yaah.cli import _outcome_json as cli_json
    for out in (Done(Envelope("done", {"raw": "ok"}), "b1"),
                Suspended("b2", "a:b", [{"k": "v"}], "ask"),
                Cleared("b3", "do", payload={"p": 1})):
        assert cli_json(out) == mcp_json(out) == out.to_json_dict(), out


def main() -> None:
    test_failure_data_additive_default()
    test_failure_data_survives_envelope_roundtrip()
    test_render_unfilled_puts_key_list_in_data()
    test_stage_failed_to_failure_json()
    test_outcome_to_json_dict_single_source()
    test_cli_and_mcp_outcome_json_agree()
    print("ok")


if __name__ == "__main__":
    main()
