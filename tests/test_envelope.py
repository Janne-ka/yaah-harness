"""Envelope contract: reply chaining, standard headers, Kind, verdict round-trip.

Run: cd yaah && PYTHONPATH=src python3 tests/test_envelope.py
"""
from __future__ import annotations

from yaah import Envelope, Failure, Kind, Verdict


def main() -> None:
    task = Envelope(Kind.TASK, {"x": 1}, headers={"baton": "b1"})

    # reply continues the chain: correlation anchored to the first id, causation = parent
    r = task.reply(Kind.RESULT, sender="role:w", y=2)
    assert r.kind == Kind.RESULT
    assert r.correlation_id == task.id, "correlation anchored to the first envelope"
    assert r.causation_id == task.id, "causation is the parent id"
    assert r.baton == "b1", "baton carried forward"
    assert r.sender == "role:w"
    assert r.payload == {"y": 2}, "reply payload is only the new kwargs"

    # a reply off a reply keeps the same correlation_id, updates causation
    r2 = r.reply(Kind.RESULT, z=3)
    assert r2.correlation_id == task.id
    assert r2.causation_id == r.id

    # Verdict carried as an Envelope, preserving the chain when given the input
    v = Verdict.failed(Failure("c", "m", "fix")).to_envelope(r)
    assert v.kind == Kind.VERDICT
    assert v.correlation_id == task.id
    back = Verdict.from_envelope(v)
    assert not back.ok and back.failures[0].code == "c" and back.failures[0].fix_hint == "fix"

    # default correlation_id falls back to the envelope's own id
    lone = Envelope(Kind.EVENT, {"n": 1})
    assert lone.correlation_id == lone.id and lone.causation_id is None

    # L2: a malformed/ERROR validator reply (no "status") -> clean hard fail, not KeyError
    malformed = Verdict.from_envelope(Envelope(Kind.ERROR, {"oops": True}))
    assert not malformed.ok and malformed.severity == "hard"

    # M4 foot-gun (slop-fix #9): a payload key named "sender" must stay in the PAYLOAD.
    # reply_with treats the payload as opaque data, so nodes spread through it; the old
    # reply(**payload) would have bound the `sender` KWARG instead — dropping it from the
    # payload and lifting it into the header. This is why the nodes were converted.
    collide = {"sender": "carol", "verdict": "ship"}
    good = task.reply_with(Kind.RESULT, collide)
    assert good.payload == {"sender": "carol", "verdict": "ship"}, good.payload
    bad = task.reply(Kind.RESULT, **collide)            # the foot-gun, kept here to document it
    assert "sender" not in bad.payload and bad.sender == "carol", (bad.payload, bad.sender)

    print("ok")


if __name__ == "__main__":
    main()
