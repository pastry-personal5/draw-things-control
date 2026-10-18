"""The ring's entries: numbers and fixed labels only, so nothing a person wrote can reach the file."""

from __future__ import annotations

import json
import unittest
from copy import deepcopy
from dataclasses import replace
from typing import Any

from framecontrol.jobs.timing.features import HistoryRun, HistoryStage
from framecontrol.jobs.timing.ring import ENTRY_KEYS, MAX_RING_MARKS, STAGE_LABELS, WORKLOAD_KEYS, fingerprint, history_run, ring_entry, valid_entry
from tests.timing.builders import dt_run, dt_workload, h3_run, h3_workload

SECRET = "/Users/someone/private/a prompt about my cat.png"


def walk(value: Any):
    """Every key and string anywhere in a JSON-like value."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)
    else:
        yield value


class RingEntryTests(unittest.TestCase):
    def test_a_live_run_becomes_an_entry_and_comes_back_as_the_same_run(self) -> None:
        run = dt_run(3, 2, step=12.0, output_frames=33, workload=dt_workload(frames=33, steps=8))
        entry = ring_entry(run)
        assert entry is not None
        self.assertTrue(valid_entry(entry))
        restored = history_run(json.loads(json.dumps(entry)))
        self.assertEqual((restored.execution, restored.run, restored.backend, restored.pipeline, restored.output_frames), (3, 2, run.backend, run.pipeline, 33))
        self.assertEqual(dict(restored.workload), dict(run.workload))
        self.assertEqual([(stage.stage, stage.start, stage.end, stage.marks) for stage in restored.stages], [(stage.stage, round(stage.start, 3), round(stage.end, 3), stage.marks) for stage in run.stages])

    def test_the_model_is_an_opaque_fingerprint_and_the_filename_never_appears(self) -> None:
        run = dt_run(1, 1, environment={"model": "wan_v2.2_secret_name.ckpt", "executable_bytes": 5, "executable_mtime": 6.5})
        entry = ring_entry(run)
        assert entry is not None
        self.assertEqual(entry["m"], fingerprint("wan_v2.2_secret_name.ckpt"))
        self.assertNotIn("secret_name", json.dumps(entry))
        self.assertEqual(len(entry["m"]), 16)
        self.assertNotEqual(fingerprint("a"), fingerprint("b"))
        self.assertEqual(history_run(entry).environment, {"model": entry["m"], "executable_bytes": 5, "executable_mtime": 6.5})

    def test_the_start_load_is_three_numbers_or_nothing(self) -> None:
        run = dt_run(1, 1)
        entry = ring_entry(replace(run, environment={**run.environment, "load_start": [0.5, 1.0, 2.0]}))
        assert entry is not None
        self.assertEqual(entry["l"], [0.5, 1.0, 2.0])
        self.assertEqual(history_run(entry).environment["load_start"], [0.5, 1.0, 2.0])
        for odd in ([1.0], "high", [1, 2, "x"], None, [float("nan"), 1, 2]):
            other = ring_entry(replace(run, environment={**run.environment, "load_start": odd}))
            assert other is not None
            self.assertIsNone(other["l"])
        self.assertFalse(valid_entry({**entry, "l": ["a", "b", "c"]}))

    def test_a_workload_keeps_only_whitelisted_numbers_flags_and_labels(self) -> None:
        workload = {**h3_workload(), "prompt": SECRET, "input": SECRET, "path": SECRET, "token_reduction": False, "reference_image_size": SECRET, "knobs": {"seconds": 15, "seed": 4, "reference_pair": SECRET, "steps": 30, "label": SECRET, "Bad Name": 1}, "compatibility": {"x": True}, "references": {"image": 3, SECRET: 2}}
        entry = ring_entry(h3_run(1, 1, workload=workload))
        assert entry is not None
        stored = entry["w"]
        self.assertLessEqual(set(stored), WORKLOAD_KEYS)
        self.assertEqual(stored["knobs"], {"steps": 30})
        self.assertEqual(stored["references"], {"image": 3})
        self.assertEqual(stored["compatibility"], True)
        self.assertNotIn("reference_image_size", stored)
        self.assertNotIn(SECRET, json.dumps(entry))

    def test_an_unknown_stage_is_the_fixed_other_label_with_its_duration(self) -> None:
        run = dt_run(1, 1)
        odd = HistoryStage(f"stage of {SECRET}", "child", 300.0, 310.0)
        entry = ring_entry(replace(run, stages=(*run.stages, odd)))
        assert entry is not None
        labels = [stage[0] for stage in entry["s"]]
        self.assertEqual(labels.count("other"), 1)
        self.assertTrue(set(labels) <= STAGE_LABELS)
        self.assertNotIn(SECRET, json.dumps(entry))
        self.assertEqual(next(stage for stage in entry["s"] if stage[0] == "other")[2:4], [300.0, 310.0])

    def test_marks_are_thinned_to_the_ring_limit(self) -> None:
        marks = tuple((float(index), index) for index in range(1000))
        run = dt_run(1, 1)
        long = replace(run, stages=(replace(run.stages[0], marks=marks, unit="frames"), *run.stages[1:]))
        entry = ring_entry(long)
        assert entry is not None
        self.assertEqual(len(entry["s"][0][7]), MAX_RING_MARKS)
        self.assertEqual((entry["s"][0][7][0], entry["s"][0][7][-1]), ([0.0, 0], [999.0, 999]))

    def test_log_rows_and_unknown_pipelines_do_not_enter(self) -> None:
        self.assertIsNone(ring_entry(replace(dt_run(1, 1), fidelity="log")))
        self.assertIsNone(ring_entry(replace(dt_run(1, 1), pipeline="mystery")))
        self.assertIsNone(ring_entry(replace(dt_run(1, 1), pipeline=None)))
        self.assertIsNone(ring_entry(replace(dt_run(1, 1), stages=())))

    def test_every_key_of_every_entry_is_on_the_whitelist(self) -> None:
        entries = [ring_entry(dt_run(1, 1)), ring_entry(h3_run(2, 1))]
        for entry in entries:
            assert entry is not None
            self.assertEqual(set(entry), ENTRY_KEYS)
            self.assertLessEqual(set(entry["w"]), WORKLOAD_KEYS)
            self.assertLessEqual(set(entry["x"]), {"bytes", "mtime"})
            for stage in entry["s"]:
                self.assertEqual(len(stage), 9)
                self.assertIn(stage[0], STAGE_LABELS)
                self.assertIn(stage[1], ("child", "framecontrol"))


class StrictValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        entry = ring_entry(dt_run(1, 1))
        assert entry is not None
        self.entry = entry

    def mutated(self, change: Any) -> dict[str, Any]:
        copy = deepcopy(self.entry)
        change(copy)
        return copy

    def test_an_extra_key_anywhere_rejects_the_entry(self) -> None:
        self.assertTrue(valid_entry(self.entry))
        self.assertFalse(valid_entry(self.mutated(lambda e: e.update(prompt="a cat"))))
        self.assertFalse(valid_entry(self.mutated(lambda e: e["w"].update(prompt="a cat"))))
        self.assertFalse(valid_entry(self.mutated(lambda e: e["x"].update(path="/tmp"))))
        self.assertFalse(valid_entry(self.mutated(lambda e: e["s"][0].append("log line"))))

    def test_a_wrong_type_or_label_rejects_the_entry(self) -> None:
        self.assertFalse(valid_entry(self.mutated(lambda e: e.update(m="wan_model.ckpt"))))
        self.assertFalse(valid_entry(self.mutated(lambda e: e.update(p="not-a-pipeline"))))
        self.assertFalse(valid_entry(self.mutated(lambda e: e.update(b="Some Backend/../x"))))
        self.assertFalse(valid_entry(self.mutated(lambda e: e["s"][0].__setitem__(0, "my file.png"))))
        self.assertFalse(valid_entry(self.mutated(lambda e: e["s"][0].__setitem__(1, "elsewhere"))))
        self.assertFalse(valid_entry(self.mutated(lambda e: e["s"][1].__setitem__(2, "soon"))))
        self.assertFalse(valid_entry(self.mutated(lambda e: e["w"].update(width=True))))
        self.assertFalse(valid_entry(self.mutated(lambda e: e["w"].update(pipeline={"x": 1}))))
        self.assertFalse(valid_entry(self.mutated(lambda e: e.update(e=-1))))
        self.assertFalse(valid_entry("a string"))
        self.assertFalse(valid_entry(None))

    def test_a_workload_with_a_hidden_string_is_not_whitelisted(self) -> None:
        self.assertFalse(valid_entry(self.mutated(lambda e: e["w"].update(mode=SECRET))))
        self.assertFalse(valid_entry(self.mutated(lambda e: e["w"].update(knobs={"label": SECRET}))))

    def test_no_string_in_a_valid_entry_is_free_text(self) -> None:
        allowed = STAGE_LABELS | {"child", "framecontrol", "draw-things-cli", "i2v", "steps", "frames"} | WORKLOAD_KEYS | ENTRY_KEYS | {"x", "bytes", "mtime", "mode", "pipeline", "image"}
        for item in walk(self.entry):
            if isinstance(item, str) and item != self.entry["m"]:
                self.assertIn(item, allowed | set(self.entry["w"].get("knobs", {})))


class HistoryRunBackTests(unittest.TestCase):
    def test_a_rebuilt_run_is_a_live_complete_history_run(self) -> None:
        entry = ring_entry(h3_run(4, 2))
        assert entry is not None
        run = history_run(entry)
        self.assertIsInstance(run, HistoryRun)
        self.assertEqual((run.fidelity, run.order), ("live", (4, 2)))
