"""contract — the @provides authoring decorator + opt-in extractor (ADR-0005 slice D).

A transform's output keys are typed PYTHON. Rather than hand-duplicate them in the pipeline
config's `provides` (which the requires↔provides lint needs to see across an
envelope-transform), annotate the function once:

    from yaah.contract import provides

    @provides("verdict", "summary")
    def review(envelope, config):
        ...
        return {"verdict": v, "summary": s}

The decorator only RECORDS the declared keys on the function object (it does not wrap or
change behaviour). An opt-in resolver (`fn_provides_resolver`) reads them so the lint can see
across the transform WITHOUT the author also writing `provides` in config — one declaration
instead of two, living next to the code it describes.

HONEST SCOPE (the keys are still hand-maintained): the decorator records the AUTHOR's declared
added-keys; it is not introspected against the actual `return` dict, so it can drift like an
`output_schema` — over-declare (a key the fn may not emit → safe false-NEGATIVE downstream) or
under-declare (a key the fn DOES emit but isn't listed → a false POSITIVE: a downstream
`{{that_key}}` is wrongly flagged). Same contract-completeness caveat as config `provides`; the
warning flags a declared-contract gap, it does not read the function body.

SHAPE ONLY (ADR-0005 slice D): keys, not obligations (NonEmpty etc. — that's slice C, deferred
until it has runtime teeth).

DOMAIN-FREE + OPT-IN (ADR-0001, [[feedback_ai_layered_optional_replaceable]]): this is an
authoring helper, not engine core. The lint NEVER imports app code on its own — a caller must
explicitly pass a resolver (so `yaah validate` stays pure by default; `--from-code` opts in).
Remove this module and the engine still works, reading config-declared `provides`.

Targets Python 3.9+.
"""
from __future__ import annotations

import sys
from typing import Any, Callable, List, Optional, Tuple

_ATTR = "__yaah_provides__"


def provides(*keys: str) -> Callable[[Any], Any]:
    """Record the payload keys a transform fn guarantees onto the payload. Raises on a
    non-string/empty key so a typo fails at import, not silently downstream."""
    for k in keys:
        if not isinstance(k, str) or not k:
            raise ValueError("@provides keys must be non-empty strings, got {!r}".format(k))

    def decorate(fn: Any) -> Any:
        setattr(fn, _ATTR, tuple(keys))
        return fn
    return decorate


def provides_of(fn: Any) -> Optional[Tuple[str, ...]]:
    """The keys recorded by @provides on `fn`, or None if undecorated."""
    keys = getattr(fn, _ATTR, None)
    return keys if isinstance(keys, tuple) else None


def fn_provides_resolver(base_dir: Optional[str] = None) -> Callable[[Any], Optional[List[str]]]:
    """Build a resolver `(transform_target) -> Optional[list[str]]` that imports an `fn:`
    target and reads its `@provides`. IMPORTS APP CODE, so wire it only when the caller has
    opted into reading contracts from code (e.g. `yaah validate --from-code`). A non-`fn:`
    target, an undecorated fn, or ANY import failure resolves to None — the lint then treats
    the transform as undeclared (its existing, safe behaviour). `base_dir` is APPENDED to
    sys.path (not prepended like the runtime's `insert(0)`) so a name-colliding app module can
    never shadow a stdlib/3rd-party one during the import — if it doesn't resolve, the transform
    just stays undeclared (a safe false-negative on this opt-in path)."""
    from .external_call import import_callable

    def resolve(target: Any) -> Optional[List[str]]:
        if not isinstance(target, str) or not target.startswith("fn:"):
            return None
        added = False
        if base_dir and base_dir not in sys.path:
            sys.path.append(base_dir)   # APPEND, not insert(0): never shadow a stdlib/3rd-party
            added = True                # module a transitively-imported module needs
        try:
            fn = import_callable(target[len("fn:"):])
        except (Exception, SystemExit):
            # unimportable / not 'module:func' / a module that sys.exit()s at import → can't
            # resolve, stay undeclared. Catch SystemExit too (a BaseException) so the lint NEVER
            # raises on the opt-in path; KeyboardInterrupt is deliberately NOT caught (Ctrl-C
            # must still abort `yaah validate`).
            return None
        finally:
            if added:
                try:
                    sys.path.remove(base_dir)
                except ValueError:
                    pass
        keys = provides_of(fn)
        return list(keys) if keys is not None else None

    return resolve
