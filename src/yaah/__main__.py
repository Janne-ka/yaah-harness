"""Entry point so `python -m yaah` works the same as the `yaah` console script.

Used by: developers running from a source checkout without `pip install`.
Where: invoked by the Python interpreter when `python -m yaah` is run.
Why: `pyproject.toml` declares `yaah = "yaah.cli:main"` as the console-script
entry point; this file makes the package directly executable via `-m` without
repeating the dispatch logic.

Targets Python 3.9+.
"""
from __future__ import annotations

from yaah.cli import main

if __name__ == "__main__":
    main()
