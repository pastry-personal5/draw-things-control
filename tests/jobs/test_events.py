"""Tests for the job event observer helpers."""

import json
import unittest
from dataclasses import fields, replace
from pathlib import Path
from typing import Any
from unittest import mock

from draw_things_control.core.cooldown import CooldownPolicy
from draw_things_control.jobs.events import EVENT_KINDS, CooldownEnded, CooldownStarted, FirstImageDropped, JobEvent, JobFinished, JobStarted, JobStatus, MediaChecked, RunFinished, RunOutput, RunStarted, RunStatus, combine_observers, event_from_dict, event_to_dict, notify
from draw_things_control.jobs.parsing import load_job
from draw_things_control.jobs.planning import PlannedRun
from tests.fixtures import JobTestCase, job_data, job_executor, run_job_with
from tests.jobs.test_executor import FakeResult, FakeRunner


class ObserverTests(unittest.TestCase):
    EVENT = CooldownEnded(at="2026-09-25T10:00:00+00:00", waited_seconds=1.0, cut_short=False)

    def test_notify_swallows_an_observer_error(self) -> None:
        def broken(_event: object) -> None:
            raise RuntimeError("display failed")

        notify(broken, self.EVENT)

    def test_notify_does_not_swallow_a_keyboard_interrupt(self) -> None:
        def interrupted(_event: object) -> None:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            notify(interrupted, self.EVENT)

    def test_combined_observers_run_in_order_and_isolate_failures(self) -> None:
        seen: list[str] = []

        def broken(_event: object) -> None:
            seen.append("broken")
            raise RuntimeError("display failed")

        combine_observers(lambda _event: seen.append("first"), broken, lambda _event: seen.append("last"))(self.EVENT)
        self.assertEqual(seen, ["first", "broken", "last"])


class EventJsonTests(unittest.TestCase):
    AT = "2026-09-27T10:00:00+00:00"

    def every_event(self) -> list[JobEvent]:
        return [
            JobStarted(at=self.AT, job_name="walk", job_file="/jobs/walk.yaml", source_text="name: walk\n", mode="i2v", total_runs=2, output_directory="/out", input="/in/a.png", model="m.ckpt", seed=7, seed_source="config_file", cooldown=CooldownPolicy(mode="manual", seconds=90), cooldown_source="job", manifest=None, log=None, config_override={"steps": 8}, input_resize={"target": [832, 448]}, execution_id="E0001"),
            RunStarted(at=self.AT, number=1, total=2, pair="walk", positive="p", negative=None, input="/in/a.png", resized_input=None, output="o.mov", last_frame="o-last-frame.png", command=("draw-things-cli", "generate", "--api-key", "[redacted]")),
            RunOutput(at=self.AT, number=1, stream="stdout", text="3/8", progress=(3, 8), percent=None),
            RunFinished(at=self.AT, number=1, status=RunStatus.SUCCEEDED, exit_code=0, seconds=12.5, output="o.mov", last_frame=None, output_width=832, output_height=448, output_frames=81),
            CooldownStarted(at=self.AT, after_run=1, seconds=90.0, until="10:01:30", mode="manual"),
            CooldownEnded(at=self.AT, waited_seconds=90.0, cut_short=False),
            MediaChecked(at=self.AT, run=1, stage="output", file="o.mov", summary="ProRes 4444 (ap4h)", verdict="warning", notes=("A note.",), facts={"matrix_scores": {"bt709": 0.216}, "colr": None}),
            FirstImageDropped(at=self.AT, reason="Run 1 failed, so no first image is kept."),
            JobFinished(at=self.AT, status=JobStatus.INTERRUPTED, exit_code=130, completed_runs=1, total_runs=2, signal="SIGINT"),
        ]

    def test_every_event_type_is_json_with_its_kind(self) -> None:
        events = self.every_event()
        self.assertEqual({type(event) for event in events}, set(EVENT_KINDS))
        kinds = []
        for event in events:
            data = event_to_dict(event)
            self.assertEqual(json.loads(json.dumps(data)), data)
            kinds.append(data["kind"])
        self.assertEqual(kinds, ["job_started", "run_started", "run_output", "run_finished", "cooldown_started", "cooldown_ended", "media_checked", "first_image_dropped", "job_finished"])

    def test_statuses_policies_and_tuples_become_plain_values(self) -> None:
        started, run_started, output, finished, _cooldown, _ended, _checked, _dropped, job_finished = (event_to_dict(event) for event in self.every_event())
        self.assertEqual(started["cooldown"], {"mode": "manual", "seconds": 90.0})
        self.assertEqual((run_started["command"], output["progress"]), (["draw-things-cli", "generate", "--api-key", "[redacted]"], [3, 8]))
        self.assertEqual((finished["status"], job_finished["status"]), ("succeeded", "interrupted"))
        self.assertIs(type(finished["status"]), str)

    def test_every_field_of_an_event_is_in_its_dict(self) -> None:
        for event in self.every_event():
            self.assertEqual(set(event_to_dict(event)), {"kind", *(field.name for field in fields(event))})

    def test_event_from_dict_round_trips_every_event_kind(self) -> None:
        for event in self.every_event():
            self.assertEqual(event_from_dict(json.loads(json.dumps(event_to_dict(event)))), event)


class EventsOfARealJobTests(JobTestCase):
    @staticmethod
    def extract(video: Path, png: Path) -> None:
        png.write_bytes(b"png")

    def test_a_job_with_a_credential_in_its_command_emits_no_credential(self) -> None:
        secret = "hunter2-secret"
        executor = job_executor(runner_factory=lambda arguments, *_rest: FakeRunner(arguments, FakeResult(), write_output=True), find_executable=lambda name: name, frame_extractor=self.extract, require_ffmpeg=lambda: "ffmpeg", handle_signals=False)
        plan_run = executor._planner.plan_run

        def with_a_key(*arguments: Any, **options: Any) -> PlannedRun:
            run = plan_run(*arguments, **options)
            return replace(run, arguments=replace(run.arguments, cloud_compute=True, api_key=secret))

        events: list[JobEvent] = []
        job = load_job(self.write_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}])), self.global_config, self.params)
        with mock.patch.object(executor._planner, "plan_run", with_a_key):
            run_job_with(executor, job, executable="draw-things-cli", shutdown_grace=1, observer=events.append)
        text = json.dumps([event_to_dict(event) for event in events])
        self.assertIn("--api-key", text)
        self.assertNotIn(secret, text)
