"""yaah.configs.bases — the R14-seed base configs as packaged resources.

A real subpackage (not just a data dir) so `importlib.resources.files(
'yaah.configs.bases')` resolves it identically from the source tree and an
installed wheel. The `*.json` seeds ride along via `[tool.setuptools.package-data]`.

Targets Python 3.9+.
"""
