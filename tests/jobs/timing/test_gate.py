from __future__ import annotations

import unittest
from dataclasses import replace

from framecontrol.jobs.timing.factory import factory_calibration, factory_tunables
from framecontrol.jobs.timing.gate import compare_replays, coverage_regressed, decide, paired_bootstrap, partition, replay
from tests.timing.builders import H3, h3_run


class GateTests(unittest.TestCase):
    def test_partition_holds_out_newest_post_checkpoint_executions(self) -> None:
        entries = [{"e": execution, "r": 1} for execution in range(1, 17)]
        split = partition(entries, 8)
        self.assertIsNone(split.reason)
        self.assertEqual({item["e"] for item in split.holdout}, {13, 14, 15, 16})
        self.assertTrue(all(item["e"] <= 12 for item in split.training))

    def test_small_holdout_cannot_be_clear_even_with_a_unanimous_material_sign(self) -> None:
        for differences in ((-0.02, -0.02, -0.02), (0.02, 0.02, 0.02)):
            with self.subTest(differences=differences):
                self.assertEqual(decide(differences, (41, 42, 43)).verdict, "inconclusive")

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

    def test_a_higher_confidence_interval_contains_the_gate_interval(self) -> None:
        differences = (-0.09, -0.05, -0.03, -0.01, 0.02, 0.04, 0.08, 0.11)
        executions = tuple(range(1, 9))

        gate = paired_bootstrap(differences, executions)
        search = paired_bootstrap(differences, executions, confidence=0.99)

        self.assertLessEqual(search[0], gate[0])
        self.assertGreaterEqual(search[1], gate[1])

    def test_full_replay_scores_remaining_time_and_coverage_per_execution(self) -> None:
        runs = [h3_run(execution, 1, step=140.0, tail=190.0) for execution in range(1, 6)]
        score = replay(runs, factory_tunables(), factory_calibration())
        self.assertTrue(score.valid)
        self.assertEqual((set(score.errors), set(score.coverage_distance), score.observations), (set(range(1, 6)), set(range(1, 6)), 30))
        self.assertTrue(all(0 <= value < 1 for value in score.errors.values()))

    def test_replay_comparison_uses_the_same_executions_and_coverage_can_veto(self) -> None:
        runs = [h3_run(execution, 1, step=140.0, tail=190.0) for execution in range(1, 9)]
        factory = factory_calibration()
        pipeline = factory.backends[H3].pipelines["r2av"]
        narrow = replace(pipeline, spread=0.01, stages=tuple(replace(stage, sigma=0.01) for stage in pipeline.stages))
        backend = replace(factory.backends[H3], pipelines={**factory.backends[H3].pipelines, "r2av": narrow})
        comparison = compare_replays(runs, factory_tunables(), factory, factory_tunables(), replace(factory, backends={**factory.backends, H3: backend}))
        self.assertEqual(comparison.execution_ids, tuple(range(1, 9)))
        self.assertEqual(len(comparison.error_differences), 8)
        self.assertIsInstance(coverage_regressed(comparison), bool)

    def test_a_valid_small_replay_is_insufficient_not_an_invalid_candidate(self) -> None:
        runs = [h3_run(execution, 1, step=140.0, tail=190.0) for execution in range(1, 3)]
        factory = factory_calibration()

        comparison = compare_replays(runs, factory_tunables(), factory, factory_tunables(), factory)

        self.assertTrue(comparison.valid)
        result = decide(comparison.error_differences, comparison.execution_ids, valid=comparison.valid)
        self.assertEqual((result.verdict, result.reason), ("inconclusive", "insufficient_evidence"))
        self.assertFalse(coverage_regressed(comparison))
