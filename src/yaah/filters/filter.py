"""Filter — the port a model-bound `envelope_get` filter implements (R10).

Used by: `envelope_tool.make_envelope_get_tool` looks up a name in the agent's
`filters` allow-list when the model invokes envelope_get with a
`filter: {name, ...params}` spec.
Where: agent config — `filters:` declares which Filter components an agent's
model may name; the model picks one by name and supplies allowed params, never
the impl. Mirrors the engine/adapters split: this port is CORE, every concrete
filter lives in `yaah/adapters/filters/`.
Why: a model that wants ±N lines around a keyword, or a redaction over a value,
expresses it through a typed, allow-listed port instead of pushing logic
through string templates. The contract is intentionally tiny so any object
(stateful class, call-target bridge, …) can implement it.

Targets Python 3.9+.
"""
from __future__ import annotations

from abc import abstractmethod
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Filter(Protocol):
    @abstractmethod
    async def apply(self, value: Any, **params: Any) -> Any:
        ...
