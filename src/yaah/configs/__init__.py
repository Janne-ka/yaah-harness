"""yaah.configs — packaged config seeds shipped inside the wheel.

`configs/bases/*.json` are the R14-seed base deployment configs (local / nats /
trace-audit). They live INSIDE the package so a `pip install yaah` (and the
public extraction) ships them: a user's root references one as
`_extends: "yaah:bases/local.base.json"`, resolved via importlib.resources by
`runtime_factories._resolve_pkg_ref` — works the same from the source tree and
from an installed wheel.

Targets Python 3.9+.
"""
