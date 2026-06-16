"""PrefixRouter — dispatch a 'name:rest' key to one of several backends.

Used by: every Routing* class (RoutingPromptSource / RoutingDataSource /
RoutingDataSink / RoutingMcpSource / RoutingBackend). They all do the SAME thing —
split a config string on the first ':', fall back to a default when there's no
prefix, look up the named backend, and raise a uniform LookupError when it's
missing — and differ ONLY in the verb they forward (get / fetch / store /
complete) and the noun in their error messages.
Where: the seam where a single config string selects which backend serves a
prompt / data read / data write / mcp config / model.
Why: name the dispatch pattern ONCE. A subclass overrides two class attributes
(`label`, `prefix`) and forwards its verb to `_select()`; the routing logic is
inherited, not re-implemented per layer.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Dict, Generic, Optional, Tuple, TypeVar

T = TypeVar("T")  # the backend type this router dispatches to


class PrefixRouter(Generic[T]):
    # Subclasses set these so the LookupError reads in the layer's own terms,
    # e.g. label="prompt source", prefix="source" -> "no prompt source 'x'".
    label = "target"
    prefix = "name"

    def __init__(self, targets: Dict[str, T], *, default: Optional[str] = None) -> None:
        self._targets = dict(targets)
        self._default = default

    def _select(self, key: Optional[str]) -> Tuple[T, str]:
        """Resolve a 'name:rest' key to (backend, rest). No prefix -> the default
        backend with the whole key as rest. Unknown name / no default -> LookupError."""
        name, sep, rest = (key or "").partition(":")
        if sep == "":  # no 'name:' prefix -> fall back to the default backend
            if self._default is None:
                raise LookupError(
                    "{} key {!r} has no {!r} prefix and no default".format(
                        self.label, key, self.prefix + ":"))
            name, rest = self._default, (key or "")
        target = self._targets.get(name)
        if target is None:
            raise LookupError(
                "no {} {!r} (key={!r}); have {}".format(
                    self.label, name, key, sorted(self._targets)))
        return target, rest
