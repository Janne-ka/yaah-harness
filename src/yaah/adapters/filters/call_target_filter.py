"""CallTargetFilter — bridge a `fn:`/`node:`/`http:` target into the Filter
port (R10 EXTENDED).

Used by: an agent's envelope_get with `filter: {name: <name>}` after the author
wires `filters: {<name>: {type: "call_target", target: "fn:app.transforms:my_trim"}}`.
Where: when the author already has a transform/node/http endpoint that does the
filtering and wants to reuse it without writing a new Filter class.
Why: the existing `call_target` resolver IS the universal escape hatch — fn,
node, or http; wrapping it in the Filter port lets call-target filters compose
with native Filter adapters in one chain. This is ONE adapter, not the
definition of the port.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Optional

from ...external_call import call_target
from ...filters import Filter


class CallTargetFilter(Filter):
    def __init__(self, target: str, *, comms: Optional[Any] = None) -> None:
        """Wrap a `fn:`/`node:`/`http:` target as a Filter.

        Args:
            target: a call_target string (`fn:module:func` / `node:role` /
                `http://…`). SECURITY: this is the universal escape hatch —
                `fn:` imports arbitrary Python and runs it (RCE-equivalent for
                an untrusted config), `http:` calls an arbitrary URL. The
                author MUST vet the target the same way any code reference in
                a config file would be vetted.
            comms: needed for `node:` targets; None is fine for `fn:`/`http:`.
        """
        self._target = target
        self._comms = comms

    async def apply(self, value: Any, **params: Any) -> Any:
        args = {"value": value}
        args.update(params)
        return await call_target(self._target, args, comms=self._comms)
