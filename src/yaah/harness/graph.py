"""Graph — the set of stages plus the start stage.

Used by: Harness (walks it) and yaah.build.harness_from_config / build_graph
(constructs it from config).
Where: the whole pipeline shape, in one object.
Why: a simple container mapping stage names to Stages with an entry point.

Targets Python 3.9+.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from .stage import Stage


@dataclass
class Graph:
    stages: Dict[str, Stage]
    start: str
    # STICKY payload keys: after every passing stage (and every fork's reduced
    # join), any sticky key present in the stage's INPUT but missing from its
    # OUTPUT is re-folded forward. Kills the recurring dropped-key defect class
    # (assessment H5: payload-replacing nodes + hand-maintained carry lists =
    # a key eventually forgotten — task, then dossier, then workdir/repo_root).
    # Fill-if-missing only: a stage that deliberately SETS a sticky key wins,
    # and a key consumed by design (e.g. concerns_from's pop) must simply not
    # be listed here.
    sticky: List[str] = field(default_factory=list)

    @classmethod
    def of(cls, *stages: Stage) -> "Graph":
        if not stages:
            raise ValueError("a graph needs at least one stage")
        return cls(stages={s.name: s for s in stages}, start=stages[0].name)
