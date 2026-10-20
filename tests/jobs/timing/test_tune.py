from __future__ import annotations

import unittest

from framecontrol.jobs.timing.factory import SEARCH_GRIDS
from framecontrol.jobs.timing.knobs import BOUNDS, SLOPE_LEVELS, Tunables
from framecontrol.jobs.timing.tune import neighbours


class SearchNeighbourTests(unittest.TestCase):
    def test_every_search_point_is_supported_and_inside_its_bound(self) -> None:
        self.assertNotIn("factory_spread", SEARCH_GRIDS)
        for name, grid in SEARCH_GRIDS.items():
            low, high = BOUNDS[name]
            self.assertEqual(tuple(sorted(set(grid))), grid)
            self.assertTrue(all(low <= value <= high for value in grid), name)
        self.assertEqual(SEARCH_GRIDS["slope_level"], SLOPE_LEVELS)

    def test_factory_neighbours_move_one_dimension_one_grid_step(self) -> None:
        base = Tunables()
        candidates = neighbours(base)
        self.assertEqual(len(candidates), 13)
        for name, candidate in candidates:
            changed = {field for field in SEARCH_GRIDS if getattr(candidate, field) != getattr(base, field)}
            self.assertEqual(changed, {name})
            grid = SEARCH_GRIDS[name]
            self.assertEqual(abs(grid.index(getattr(candidate, name)) - grid.index(getattr(base, name))), 1)

    def test_pins_and_measured_rho_remove_those_dimensions(self) -> None:
        names = {name for name, _candidate in neighbours(Tunables(), {"pause_factor"}, rho_measured=True)}
        self.assertNotIn("pause_factor", names)
        self.assertNotIn("rho", names)


if __name__ == "__main__":
    unittest.main()
