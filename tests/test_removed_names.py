"""The step-counter estimator is gone for good: its names are not in the source, and the wire keeps its numbers closed."""

from __future__ import annotations

import re
import unittest
from dataclasses import fields
from pathlib import Path

from framecontrol.core.process.output import ProcessMessage
from framecontrol.jobs.events import RunOutput
from framecontrol.server.generated import monitor_pb2

SOURCE = Path(__file__).resolve().parents[1] / "src" / "framecontrol"
PROTO = SOURCE / "server" / "proto" / "monitor.proto"
# What Milestone 11 removed from the product: the global step counter, and the heuristics that read it.
REMOVED = ("current_step", "current_step_total", "PROGRESS_PATTERN", "PERCENT_PATTERN", "StepReading", "PastRun", "latest_past_run", "read_past_run", "latest_succeeded_run", "progress_text", "full_seconds", "tail_seconds", "forget_progress")


class RemovedNamesTests(unittest.TestCase):
    def test_no_removed_name_remains_in_the_source(self) -> None:
        pattern = re.compile("|".join(rf"\b{name}\b" for name in REMOVED))
        found = []
        for path in sorted([*SOURCE.rglob("*.py"), PROTO]):
            if "generated" in path.parts:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                # The proto keeps the two names reserved, so that no field is ever given them again.
                if pattern.search(line) and not (path == PROTO and line.strip().startswith("reserved")):
                    found.append(f"{path.relative_to(SOURCE)}:{number}: {line.strip()}")
        self.assertEqual(found, [])

    def test_a_run_output_event_and_a_process_message_carry_no_progress_or_percent(self) -> None:
        for kind in (RunOutput, ProcessMessage):
            self.assertFalse({"progress", "percent"} & {field.name for field in fields(kind)}, kind.__name__)


class SnapshotWireTests(unittest.TestCase):
    def test_the_snapshot_closes_fields_8_and_9_and_their_names_and_adds_the_forecast_at_14(self) -> None:
        descriptor = monitor_pb2.QueueEntrySnapshot.DESCRIPTOR
        self.assertNotIn(8, descriptor.fields_by_number)
        self.assertNotIn(9, descriptor.fields_by_number)
        self.assertNotIn("current_step", descriptor.fields_by_name)
        self.assertEqual(descriptor.fields_by_name["forecast"].number, 14)
        text = PROTO.read_text(encoding="utf-8")
        self.assertIn("reserved 8, 9;", text)
        self.assertIn('reserved "current_step", "current_step_total";', text)

    def test_no_other_field_of_the_snapshot_moved(self) -> None:
        numbers = {field.name: field.number for field in monitor_pb2.QueueEntrySnapshot.DESCRIPTOR.fields}
        self.assertEqual(numbers, {"queue_id": 1, "state": 2, "execution_id": 3, "current_run": 4, "current_run_elapsed_seconds": 5, "cooldown_until": 6, "error": 7, "total_runs": 10, "park_requested": 11, "queue_held": 12, "kind": 13, "forecast": 14})
