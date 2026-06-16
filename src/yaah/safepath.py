"""safe_join — resolve a config key to a real file path, with containment.

Used by: the file-backed adapters (FileDataSource / FileSink / FilePromptSource
/ FileMcpSource). Each takes a `base_dir` + a per-call `key`, then opens
`<base>/<key>`. Without containment, a key like `../../../etc/passwd` escapes
the base; FileSink + auto-`makedirs` made that the most dangerous (could write
arbitrary files anywhere the process had permission).
Where: a tiny pure-stdlib root-level module (peer of `cwd`/`jsonio`/`recall`).
Why: ONE place that defines the safe-path contract — every file adapter calls
it, so there's no "this one adapter forgot to check" failure mode. Allow
absolute keys (the operator may legitimately point at a path outside `base`,
config-trusted), but reject relative keys that resolve OUTSIDE `base`.

Targets Python 3.9+.
"""
from __future__ import annotations

import os
from typing import Optional


def safe_join(base: Optional[str], key: str, *, allow_absolute: bool = True) -> str:
    """Resolve `key` (relative or absolute) to a file path with traversal
    containment. Returns the path callers would have constructed themselves
    (unresolved symlinks, no canonicalization) so downstream code sees a
    predictable value; the safety check is performed via realpath but the
    canonical form is NOT returned.

    Absolute keys pass through when `allow_absolute=True` (operator's explicit
    intent; trusted-config model). Set `allow_absolute=False` for adapters that
    must NEVER escape `base` (e.g. an HTTP-exposed sink).

    `base=None` or empty means "cwd-relative, no containment to enforce" — the
    legacy default. Adapters that want containment pass their own `base_dir`.

    Containment rule (relative case): the realpath of `os.path.join(base, key)`
    must equal `realpath(base)` or be a descendant. Catches `../` traversal AND
    symlinks pointing outside the base.

    Raises ValueError when a relative key escapes `base`."""
    if os.path.isabs(key):
        if not allow_absolute:
            raise ValueError("absolute key {!r} not allowed".format(key))
        return key
    if not base:
        return key  # no containment without a base — legacy cwd-relative shape
    full = os.path.join(base, key)
    base_real = os.path.realpath(base)
    full_real = os.path.realpath(full)
    if full_real != base_real and not full_real.startswith(base_real + os.sep):
        raise ValueError(
            "key {!r} escapes base {!r} (resolved to {!r})".format(key, base, full_real))
    return full
