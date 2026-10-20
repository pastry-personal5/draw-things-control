"""Pure helpers for the constrained M15 tunables search."""

from __future__ import annotations

from collections.abc import Set
from dataclasses import replace

from framecontrol.jobs.timing.factory import SEARCH_GRIDS
from framecontrol.jobs.timing.knobs import Tunables


def neighbours(tunables: Tunables, pinned: Set[str] = frozenset(), *, rho_measured: bool = False) -> tuple[tuple[str, Tunables], ...]:
    """One-grid-step candidates, excluding pins and inactive/safety dimensions."""
    result: list[tuple[str, Tunables]] = []
    for name, grid in SEARCH_GRIDS.items():
        if name in pinned or (name == "rho" and rho_measured):
            continue
        value = float(getattr(tunables, name))
        try:
            index = grid.index(value)
        except ValueError:
            continue
        for next_index in (index - 1, index + 1):
            if 0 <= next_index < len(grid):
                result.append((name, replace(tunables, **{name: grid[next_index]})))
    return tuple(result)


__all__ = ["neighbours"]
