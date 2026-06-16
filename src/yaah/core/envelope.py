"""Envelope — the one message shape that moves through the whole system.

Used by: every node (receives one, returns one), Comms (serializes it on the
wire), and the harness (reads kind + headers, chains replies).
Where: ubiquitous — the single payload type of the kernel.
Why: one simple, serializable message; domain data in `payload`, metadata in
`headers`. Standard header keys: correlation_id, causation_id, baton, sender,
attempt, schema (payload type+version), idempotency_key, ts.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Envelope:
    kind: str
    payload: Dict[str, Any] = field(default_factory=dict)
    headers: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def correlation_id(self) -> str:
        return self.headers.get("correlation_id", self.id)

    @property
    def causation_id(self) -> Optional[str]:
        return self.headers.get("causation_id")

    @property
    def baton(self) -> Optional[str]:
        return self.headers.get("baton")

    @property
    def sender(self) -> Optional[str]:
        return self.headers.get("sender")

    @property
    def schema(self) -> Optional[str]:
        return self.headers.get("schema")

    def reply(self, kind: str, *, sender: Optional[str] = None, **payload: Any) -> "Envelope":
        """A new Envelope continuing this chain: same correlation_id and baton,
        causation_id set to this envelope's id. Payload as keyword args — for an
        ARBITRARY payload dict (whose keys might collide with `sender`), use
        reply_with()."""
        return self.reply_with(kind, payload, sender=sender)

    def reply_with(self, kind: str, payload: Dict[str, Any], *,
                   sender: Optional[str] = None) -> "Envelope":
        """Same chain-continuing reply, but the payload is a DICT, not kwargs — so a
        payload key like `sender` can't collide with the keyword arg (bug review
        M4). reply() delegates here; callers forwarding an opaque payload (e.g. a
        `node:` transform) should use this directly."""
        headers: Dict[str, Any] = {
            "correlation_id": self.correlation_id,
            "causation_id": self.id,
        }
        if "baton" in self.headers:
            headers["baton"] = self.headers["baton"]
        if "clear_id" in self.headers:  # a fork's gate id rides the branch chain to its fan-in
            headers["clear_id"] = self.headers["clear_id"]
        if sender is not None:
            headers["sender"] = sender
        return Envelope(kind=kind, payload=dict(payload), headers=headers)

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "payload": self.payload, "headers": self.headers}

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Envelope":
        # Assessment cluster 2 LOW: a wire message missing `kind` used to
        # raise a deep KeyError straight up through transport adapters. Raise
        # a structured ValueError instead — adapters can translate to a
        # Kind.ERROR reply (NatsComms.serve already does this).
        if not isinstance(d, dict) or "kind" not in d:
            raise ValueError(
                "invalid Envelope dict (missing required field 'kind'): {!r}"
                .format(d if isinstance(d, dict) else type(d).__name__))
        env = cls(
            kind=d["kind"],
            payload=dict(d.get("payload") or {}),
            headers=dict(d.get("headers") or {}),
        )
        if d.get("id"):
            env.id = d["id"]
        return env

    @classmethod
    def from_json(cls, s: str) -> "Envelope":
        return cls.from_dict(json.loads(s))
