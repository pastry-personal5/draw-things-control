"""Pins what a real job run writes and emits, so the Milestone 11 refactor cannot change it unnoticed.

Each scenario runs a job with fake tools and a fixed clock, then compares one text snapshot with a file in
``tests/jobs/golden/``: the log lines, the job's events in order, the manifest, the job log file, and the rows the
state store keeps. A change that is meant (a recorded bug fix) is accepted by running the tests once with
``DTC_UPDATE_GOLDEN=1`` and reviewing the diff of the golden files.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import signal
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

from loguru import logger

from draw_things_control.core.arguments import DrawThingsGenerateArguments
from draw_things_control.core.process.output import OutputStream
from draw_things_control.jobs import job_service
from draw_things_control.jobs.job_definition import JobDefinition, load_job
from draw_things_control.jobs.job_events import JobEvent, combine_observers
from draw_things_control.jobs.job_service import JobService
from draw_things_control.jobs.media_info import MediaInfo
from draw_things_control.state.recorder import ExecutionRecorder
from draw_things_control.state.store import Store
from tests.fixtures import JobTestCase, job_data
from tests.jobs.test_job_service import FakeResult, FakeRunner, TalkingRunner

GOLDEN = Path(__file__).parent / "golden"
NOW = datetime(2026, 9, 24, 15, 30, 12)
# A timestamp's offset depends on the machine's time zone, and the job log's clock is the real one.
OFFSET = re.compile(r"(\d{2}:\d{2}:\d{2})[+-]\d{2}:\d{2}")
TEMPORARY_COPY = re.compile(r"[^\s'\"]*/draw-things-control-\w+/")
LOG_TIME = re.compile(r"(?m)^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}")
NOT_KEPT = {"id", "execution_id", "started_epoch", "finished_epoch"}


class JobCharacterizationTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        numbers = itertools.count(1000)
        self.calls = 0
        self.results: dict[int, FakeResult] = {}
        self.talk: dict[int, tuple[tuple[OutputStream, str], ...]] = {}
        self.cooldowns: list[float] = []
        self.on_cooldown = lambda seconds: seconds
        self.on_extract = lambda video, png: None
        self.service = JobService(
            runner_factory=self.create_runner,
            find_executable=lambda executable: executable,
            frame_extractor=self.extract,
            require_ffmpeg=lambda: "ffmpeg",
            clock=lambda: NOW,
            random_number=lambda: next(numbers),
            random_seed=lambda: 777,
            handle_signals=False,
            cooldown=self.wait,
            output_measurer=lambda output: MediaInfo(832, 448, 81),
        )
        # Each run takes 12.5 s: the service reads the clock at the start and the end of a run.
        readings = itertools.count(step=12.5)
        patcher = mock.patch.object(job_service, "time", mock.Mock(monotonic=lambda: next(readings)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logged: list[str] = []
        sink = logger.add(lambda message: self.logged.append(str(message).rstrip("\n")), format="{level} {message}", level="INFO")
        self.addCleanup(logger.remove, sink)
        self.state = self.root / "state"
        self.state.mkdir()
        self.store = Store(self.state / "dtc.db", prune_on_open=False, clock=lambda: NOW)
        self.addCleanup(self.store.close)

    def create_runner(self, arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float, on_message: Any = None, on_start: Any = None) -> FakeRunner:
        self.calls += 1
        if self.calls in self.talk:
            return TalkingRunner(arguments, on_message, self.talk[self.calls])
        return FakeRunner(arguments, self.results.get(self.calls, FakeResult()), write_output=True)

    def extract(self, video: Path, png: Path) -> None:
        png.write_bytes(b"png")
        self.on_extract(video, png)

    def wait(self, seconds: float) -> float:
        self.cooldowns.append(seconds)
        return self.on_cooldown(seconds)

    def job(self, **changes: Any) -> JobDefinition:
        pairs = [{"name": "walk", "positive": "walk", "negative": "blurry", "runs": [1, 3]}, {"name": "wave", "positive": "wave", "default": True}]
        data = job_data(**{"run_count": 3, "prompt_pairs": pairs, "cooldown": {"mode": "manual", "seconds": 90}, **changes})
        return load_job(self.write_job(data), self.global_config, self.params)

    def snapshot(self, job: JobDefinition) -> str:
        events: list[JobEvent] = []
        recorder = ExecutionRecorder(self.store)
        outcome = self.service.run(job, executable="draw-things-cli", shutdown_grace=10.0, write_records=True, observer=combine_observers(recorder, events.append), reserve_execution_id=recorder.reserve)
        assert outcome.manifest is not None and outcome.log is not None
        manifest = json.loads(outcome.manifest.read_text(encoding="utf-8"))
        sections = [
            ("OUTCOME", f"exit_code={outcome.exit_code} completed_runs={outcome.completed_runs} total_runs={outcome.total_runs} manifest={outcome.manifest.name} log={outcome.log.name}"),
            ("COOLDOWN WAITS", repr(self.cooldowns)),
            ("LOG LINES", "\n".join(self.logged)),
            ("EVENTS", "\n".join(repr(event) for event in events)),
            ("MANIFEST", json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True)),
            ("JOB LOG FILE", LOG_TIME.sub("<time>", outcome.log.read_text(encoding="utf-8"))),
            ("STATE STORE", self.database()),
            ("OUTPUT FILES", "\n".join(sorted(str(path.relative_to(self.output_directory)) for path in self.output_directory.rglob("*") if path.is_file()))),
        ]
        text = "\n\n".join(f"=== {title} ===\n{body}" for title, body in sections) + "\n"
        return TEMPORARY_COPY.sub("TMPCOPY/", OFFSET.sub(r"\1+ZZ:ZZ", text.replace(str(self.root), "ROOT")))

    def database(self) -> str:
        executions = self.store.list_executions()
        rows = []
        for execution in executions:
            full = self.store.get_execution(execution["id"])
            assert full is not None
            runs = [{key: value for key, value in run.items() if key not in NOT_KEPT} for run in full.pop("runs")]
            rows.append({"execution": {key: value for key, value in full.items() if key not in NOT_KEPT}, "runs": runs})
        return json.dumps(rows, indent=2, ensure_ascii=False, sort_keys=True)

    def check(self, name: str, text: str) -> None:
        path = GOLDEN / f"{name}.txt"
        if os.environ.get("DTC_UPDATE_GOLDEN"):
            GOLDEN.mkdir(exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.assertTrue(path.exists(), f"{path} is missing; run the tests once with DTC_UPDATE_GOLDEN=1")
        self.assertEqual(text, path.read_text(encoding="utf-8"))

    def test_a_job_that_succeeds(self) -> None:
        self.talk[1] = ((OutputStream.STDOUT, "Sampling 3/8"), (OutputStream.STDERR, "a warning"))
        self.check("succeeded", self.snapshot(self.job()))

    def test_a_job_that_fails_in_run_2(self) -> None:
        self.results[2] = FakeResult(return_code=3)
        self.check("failed-in-run-2", self.snapshot(self.job()))

    def test_a_job_stopped_during_a_cooldown(self) -> None:
        def stop(seconds: float) -> float:
            self.service.cancel(signal.SIGINT)
            return 40.0

        self.on_cooldown = stop
        self.check("stopped-in-cooldown", self.snapshot(self.job()))

    def test_a_job_stopped_between_runs(self) -> None:
        self.on_extract = lambda video, png: self.service.cancel(signal.SIGTERM)
        self.check("stopped-between-runs", self.snapshot(self.job()))

    def test_a_job_with_a_cooldown_of_off_and_an_image_input_resize(self) -> None:
        self.write_image("photo.jpg", (1920, 1080))
        self.check("resized-input-no-cooldown", self.snapshot(self.job(cooldown={"mode": "off"}, input="photo.jpg", desired_input_width=850)))
