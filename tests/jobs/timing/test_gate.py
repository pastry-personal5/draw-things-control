from __future__ import annotations

import unittest

from framecontrol.jobs.timing.gate import decide, paired_bootstrap, partition


class GateTests(unittest.TestCase):
    def test_partition_holds_out_newest_post_checkpoint_executions(self) -> None:
        entries = [{"e": execution, "r": 1} for execution in range(1, 17)]
        split = partition(entries, 8)
        self.assertIsNone(split.reason)
        self.assertEqual({item["e"] for item in split.holdout}, {13, 14, 15, 16})
        self.assertTrue(all(item["e"] <= 12 for item in split.training))

    def test_small_holdout_cannot_be_clear_without_unanimous_material_sign(self) -> None:
        result = decide((-0.02, -0.02, 0.0), (41, 42, 43))
        self.assertEqual(result.verdict, "inconclusive")

    def test_no_change_is_not_promoted(self) -> None:
        self.assertEqual(decide((0.0,) * 8, tuple(range(8))).verdict, "inconclusive")

    def test_missing_checkpoint_mark_does_not_expose_old_training_executions(self) -> None:
        entries = [{"e": execution, "r": 1} for execution in range(1, 40)]
        self.assertEqual(partition(entries, None).reason, "insufficient_evidence")

    def test_bootstrap_is_reproducible_from_execution_ids(self) -> None:
        first = paired_bootstrap((-0.04, -0.02, -0.03, -0.01, -0.05), (4, 8, 15, 16, 23))
        second = paired_bootstrap((-0.04, -0.02, -0.03, -0.01, -0.05), (4, 8, 15, 16, 23))
        self.assertEqual(first, second)

    def test_bootstrap_is_invariant_to_the_order_of_execution_pairs(self) -> None:
        ordered = paired_bootstrap((-0.04, -0.02, -0.03, -0.01, -0.05), (4, 8, 15, 16, 23))
        shuffled = paired_bootstrap((-0.01, -0.04, -0.05, -0.03, -0.02), (16, 4, 23, 15, 8))
        self.assertEqual(ordered, shuffled)

    def test_bootstrap_requires_distinct_execution_pairs_and_a_sample(self) -> None:
        for differences, executions, samples in (((-0.1, -0.2), (1, 1), 100), ((-0.1,), (1,), 0)):
            with self.subTest(executions=executions, samples=samples), self.assertRaises(ValueError):
                paired_bootstrap(differences, executions, samples=samples)
