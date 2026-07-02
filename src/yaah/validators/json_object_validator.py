"""JsonObjectValidator — the JSON gate every structured-output agent needs.

Used by: the `json_object` node type (`build/builders.py:_build_json_object`).
Where: in a stage's `validators:` list, immediately after an `agent` stage,
to fail+retry when the agent's reply isn't a JSON object with the required
keys.
Why: agents return free text; structured downstream stages need structured
input. This is the cheapest typed-I/O gate the engine ships.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
from typing import List, Optional

from ..core import Node, Envelope, Failure, NodeConfig, Verdict
from ..jsonio import extract_json


class JsonObjectValidator(Node):
    """Passes if payload[key] parses as a JSON object with the required keys."""

    def __init__(self, required: Optional[List[str]] = None, *, key: str = "raw") -> None:
        self._required = list(required or [])
        self._key = key

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        raw = input.payload.get(self._key, "")
        try:
            obj = extract_json(raw)
        except json.JSONDecodeError as e:
            return Verdict.failed(Failure.not_json(e)).to_envelope(input)
        if not isinstance(obj, dict):
            return Verdict.failed(Failure(
                "not_object", "top level is not a JSON object",
                "return a JSON object")).to_envelope(input)
        missing = [k for k in self._required if k not in obj]
        if missing:
            return Verdict.failed(Failure(
                "missing_keys", "missing keys: {}".format(missing),
                "include keys {}".format(self._required))).to_envelope(input)
        return Verdict.passed().to_envelope(input)
