"""The stage timing a job's runs leave: its events, and that its stages add up to the run."""

from __future__ import annotations

import itertools
import signal
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

from framecontrol.core.arguments import DrawThingsGenerateArguments
from framecontrol.core.process.output import OutputStream, ProcessMessage
from framecontrol.jobs.definition import JobDefinition
from framecontrol.jobs.events import JobEvent, RunFinished, RunOutput, RunStarted, StageFinished, StageStarted, TimingFinished, TimingStarted
from framecontrol.jobs.executor import JobExecutor
from framecontrol.jobs.media.info import MediaInfo
from framecontrol.jobs.parsing import load_job
from tests.fixtures import JobTestCase, job_data, job_executor, load_legacy_job, run_job_with
from tests.jobs.test_executor import FakeResult, FakeRunner
from tests.jobs.timing.transcripts import FakeClock, Transcript, draw_things

NOW = datetime(2026, 9, 24, 15, 30, 12)


class PlayingRunner(FakeRunner):
    """A runner whose child printed ``transcript``: the lines reach the callback with their offsets, then the clock moves to the exit."""

    def __init__(self, arguments: DrawThingsGenerateArguments, clock: FakeClock, on_message: Any, transcript: Transcript, result: FakeResult, *, write_output: bool = True, before_exit: Any = None) -> None:
        super().__init__(arguments, result, write_output)
        self.clock, self.on_message, self.transcript, self.before_exit = clock, on_message, transcript, before_exit

    def run(self) -> FakeResult:
        for line in self.transcript.lines:
            self.on_message(ProcessMessage(stream=OutputStream(line.stream), text=line.text, elapsed_seconds=line.offset))
        self.clock.advance(self.transcript.exit_at)
        if self.before_exit is not None:
            self.before_exit()
        return super().run()


class RunClockRecorder:
    """Notes the clock at each run's RunStarted and RunFinished, which is what the stages must add up to."""

    def __init__(self, clock: FakeClock, prepare_seconds: float = 0.0) -> None:
        self.clock = clock
        # How long the run's preparation takes: the clock moves this far when its timing starts.
        self.prepare_seconds = prepare_seconds
        self.events: list[JobEvent] = []
        self.started: dict[int, float] = {}
        self.finished: dict[int, float] = {}

    def __call__(self, event: JobEvent) -> None:
        self.events.append(event)
        if isinstance(event, TimingStarted):
            self.clock.advance(self.prepare_seconds)
        if isinstance(event, RunStarted):
            self.started[event.number] = self.clock.now
        elif isinstance(event, RunFinished):
            self.finished[event.number] = self.clock.now

    def stages(self, number: int) -> list[StageFinished]:
        return [event for event in self.events if isinstance(event, StageFinished) and event.number == number]

    def counted(self, number: int) -> float:
        """The seconds of the run's stages, leaving out work done before it started."""
        return sum(event.end - event.start for event in self.stages(number) if event.start >= 0)


class ExecutorTimingTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.clock = FakeClock()
        self.transcripts: dict[int, Transcript] = {}
        self.results: dict[int, FakeResult] = {}
        self.missing_output: set[int] = set()
        self.on_exit: dict[int, Any] = {}
        self.calls = 0
        numbers = itertools.count(1000)
        self.service = job_executor(
            runner_factory=self.create_runner,
            find_executable=lambda executable: f"/fake/{executable}",
            frame_extractor=self.extract,
            require_ffmpeg=lambda: "ffmpeg",
            clock=lambda: NOW,
            random_number=lambda: next(numbers),
            random_seed=lambda: 777,
            handle_signals=False,
            cooldown=lambda seconds: seconds,
            output_measurer=self.measure,
            monotonic=self.clock,
            host_load=lambda: (1.0, 0.5, 0.25),
        )

    def create_runner(self, arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float, on_message: Any = None, on_start: object = None) -> FakeRunner:
        self.calls += 1
        number = self.calls
        return PlayingRunner(arguments, self.clock, on_message, self.transcripts.get(number, draw_things()), self.results.get(number, FakeResult()), write_output=number not in self.missing_output, before_exit=self.on_exit.get(number))

    def extract(self, video: Path, png: Path) -> None:
        png.write_bytes(b"png")
        self.clock.advance(2.0)

    def measure(self, output: Path) -> MediaInfo:
        self.clock.advance(0.5)
        return MediaInfo(832, 448, 81)

    def job(self, **changes: Any) -> JobDefinition:
        data = job_data(**{"run_count": 1, "prompt_pairs": [{"name": "only", "positive": "text"}], **changes})
        return load_legacy_job(self.write_legacy_job(data), self.global_config, self.params)

    def observe(self, job: JobDefinition, *, prepare_seconds: float = 0.0) -> RunClockRecorder:
        recorder = RunClockRecorder(self.clock, prepare_seconds)
        run_job_with(self.service, job, executable="draw-things-cli", shutdown_grace=2, write_records=True, observer=recorder)
        return recorder

    def assert_adds_up(self, recorder: RunClockRecorder, number: int) -> None:
        self.assertAlmostEqual(recorder.counted(number), recorder.finished[number] - recorder.started[number], delta=0.1)

    def test_a_draw_things_run_has_the_stages_its_output_describes_in_place_on_the_run_clock(self) -> None:
        recorder = self.observe(self.job())
        stages = {event.stage: event for event in recorder.stages(1)}
        transcript = draw_things()
        prepare = stages["prepare"]
        for expected in transcript.spans:
            actual = stages[expected.stage]
            self.assertEqual((actual.source, actual.phase, actual.done, actual.total, actual.unit), ("child", expected.phase, expected.done, expected.total, expected.unit), expected.stage)
            self.assertAlmostEqual(actual.start, prepare.end + expected.start, places=6)
            self.assertAlmostEqual(actual.end, prepare.end + expected.end, places=6)
        self.assertEqual(len(stages["sampling"].marks), 41)
        self.assert_adds_up(recorder, 1)

    def test_every_line_of_output_names_its_phase_and_when_the_child_wrote_it_on_the_runs_clock(self) -> None:
        recorder = self.observe(self.job(), prepare_seconds=0.75)
        outputs = [event for event in recorder.events if isinstance(event, RunOutput)]
        transcript = draw_things()
        # Seconds after RunStarted, like a stage's offsets: the child's own stamp, and the 0.75 s of preparation before it.
        self.assertEqual([(event.text, round(event.offset, 6)) for event in outputs], [(line.text, round(line.offset + 0.75, 6)) for line in transcript.lines])
        stages = {event.stage: event for event in recorder.stages(1)}
        sampling = next(event for event in outputs if event.text.startswith("Sampling... 1 /"))
        self.assertAlmostEqual(sampling.offset, stages["sampling"].start, places=6)
        phases = {event.text.split("...")[0].split(" ")[0]: event.phase for event in outputs}
        self.assertEqual((phases["Starting"], phases["Sampling"], phases["Wrote:"]), ("Starting", "Sampling", None))

    def test_a_stage_reading_carries_its_stage_and_counter_for_the_forecast(self) -> None:
        recorder = self.observe(self.job())
        outputs = [event for event in recorder.events if isinstance(event, RunOutput)]
        sampling = [event for event in outputs if event.text.startswith("Sampling...")]
        # Draw Things prints `Sampling... n / N` as step n starts: n - 1 steps are done, as the reading holds it.
        self.assertEqual([(event.stage, event.done, event.total) for event in sampling[:2]], [("sampling", 0, 40), ("sampling", 1, 40)])
        starting = next(event for event in outputs if event.text.startswith("Starting"))
        self.assertEqual((starting.stage, starting.done, starting.total), ("starting", None, None))
        other = next(event for event in outputs if event.text.startswith("Wrote:"))
        self.assertEqual((other.stage, other.done, other.total), (None, None, None))

    def test_the_stages_add_up_to_the_run_for_a_run_that_fails_is_stopped_or_times_out(self) -> None:
        cases = {"failed": FakeResult(return_code=3), "stopped": FakeResult(return_code=0, termination_signal=signal.SIGTERM), "timed out": FakeResult(return_code=-15, timed_out=True, termination_signal=signal.SIGTERM)}
        for name, result in cases.items():
            with self.subTest(name):
                self.calls = 0
                self.results[1] = result
                self.missing_output.add(1)
                recorder = self.observe(self.job())
                self.assert_adds_up(recorder, 1)
                (timing_finished,) = [event for event in recorder.events if isinstance(event, TimingFinished)]
                self.assertFalse(timing_finished.complete)
                self.assertNotIn("measure", [event.stage for event in recorder.stages(1)])

    def test_a_run_that_raises_closes_its_timing_before_it_finishes(self) -> None:
        def cannot_start(*_arguments: object) -> FakeRunner:
            self.clock.advance(0.75)
            raise ValueError("Could not start executable draw-things-cli: Permission denied")

        self.service.knobs.runner_factory = cannot_start
        recorder = RunClockRecorder(self.clock)
        try:
            run_job_with(self.service, self.job(), executable="draw-things-cli", shutdown_grace=2, observer=recorder)
        except ValueError:
            pass
        kinds = [type(event) for event in recorder.events if isinstance(event, TimingFinished | RunFinished)]
        self.assertEqual(kinds, [TimingFinished, RunFinished])
        self.assert_adds_up(recorder, 1)
        self.assertFalse(next(event for event in recorder.events if isinstance(event, TimingFinished)).complete)

    def test_a_successful_run_is_complete_and_the_run_a_park_waited_for_is_too(self) -> None:
        self.on_exit[1] = self.service.park
        recorder = self.observe(self.job(run_count=2))
        self.assertEqual([event.complete for event in recorder.events if isinstance(event, TimingFinished)], [True])
        self.assert_adds_up(recorder, 1)
        self.assertNotIn(2, recorder.started)

    def test_a_timing_started_event_says_what_the_run_is_judged_by(self) -> None:
        recorder = self.observe(self.job())
        (started,) = [event for event in recorder.events if isinstance(event, TimingStarted)]
        self.assertEqual((started.backend, started.pipeline, started.number), ("draw-things-cli", "i2v", 1))
        self.assertEqual({key: started.workload[key] for key in ("width", "height", "pixels", "steps")}, {"width": 832, "height": 448, "pixels": 832 * 448, "steps": 30})
        self.assertEqual(started.environment["model"], "base.ckpt")
        self.assertEqual(started.environment["load_start"], [1.0, 0.5, 0.25])
        (finished,) = [event for event in recorder.events if isinstance(event, TimingFinished)]
        self.assertEqual((finished.complete, finished.load_end), (True, (1.0, 0.5, 0.25)))

    def test_work_done_for_run_1_before_it_starts_is_a_span_before_zero_and_not_in_the_sum(self) -> None:
        self.write_image("photo.jpg", (1920, 1080))
        resize = JobExecutor._temporary_input

        def slow_resize(job: JobDefinition) -> Any:
            self.clock.advance(1.25)
            return resize(job)

        with mock.patch.object(JobExecutor, "_temporary_input", staticmethod(slow_resize)):
            recorder = self.observe(self.job(input="photo.jpg", desired_input_width=850))
        before = [event for event in recorder.stages(1) if event.start < 0]
        self.assertEqual([(event.stage, event.source, event.start, event.end) for event in before], [("prepare_input", "framecontrol", -1.25, 0.0)])
        self.assert_adds_up(recorder, 1)

    def staging(self, seconds: float) -> Any:
        """A ``prepare_job`` for the Draw Things adapter (which has none) that takes ``seconds`` on the run clock."""

        def prepare_job(_backend: object, job: JobDefinition) -> Any:
            self.clock.advance(seconds)
            return job, lambda: None

        return mock.patch.object(type(self.service.backends.get("draw-things-cli")), "prepare_job", prepare_job, create=True)

    def references(self, recorder: RunClockRecorder, number: int) -> list[tuple[float, float]]:
        return [(round(event.start, 3), round(event.end - event.start, 3)) for event in recorder.stages(number) if event.stage == "stage_references"]

    def test_a_version_2_run_is_timed_with_only_the_references_staged_for_it(self) -> None:
        job = load_job(self.write_job(job_data(run_count=3, prompt_pairs=[{"name": "only", "positive": "text"}])), self.global_config, self.params)
        with self.staging(0.4):
            recorder = self.observe(job)
        for number in (1, 2, 3):
            with self.subTest(run=number):
                ((start, took),) = self.references(recorder, number)
                self.assertLess(start, 0.0)
                self.assertEqual(took, 0.4)
                self.assert_adds_up(recorder, number)

    def test_a_version_1_jobs_staging_belongs_to_the_run_that_starts_first(self) -> None:
        with self.staging(0.4):
            recorder = self.observe(self.job(run_count=2))
        self.assertEqual([took for _start, took in self.references(recorder, 1)], [0.4])
        self.assertEqual(self.references(recorder, 2), [])

    def test_a_stage_is_announced_when_it_starts_and_again_when_it_ends(self) -> None:
        recorder = self.observe(self.job())
        started = [(event.stage, event.start) for event in recorder.events if isinstance(event, StageStarted) and event.number == 1]
        finished = [(event.stage, event.start) for event in recorder.events if isinstance(event, StageFinished) and event.number == 1]
        self.assertEqual(sorted(started), sorted(finished))
        order = [type(event) for event in recorder.events]
        self.assertLess(order.index(RunStarted), order.index(TimingStarted))
        self.assertLess(max(index for index, event in enumerate(recorder.events) if isinstance(event, StageFinished)), order.index(TimingFinished))
        self.assertLess(order.index(TimingFinished), order.index(RunFinished))

    def test_a_timing_hook_that_raises_never_stops_the_run(self) -> None:
        backend = self.service.backends.get("draw-things-cli")
        with mock.patch.object(type(backend), "timing_workload", side_effect=RuntimeError("hook bug")), mock.patch.object(type(backend), "timing_environment", side_effect=RuntimeError("hook bug")), mock.patch.object(type(backend), "stage_parser", side_effect=RuntimeError("hook bug")):
            recorder = self.observe(self.job())
        self.assertEqual([event.status for event in recorder.events if isinstance(event, RunFinished)], ["succeeded"])
        (started,) = [event for event in recorder.events if isinstance(event, TimingStarted)]
        self.assertEqual((started.workload, started.environment.get("model")), ({}, None))
        self.assertEqual([event.stage for event in recorder.stages(1) if event.source == "child"], ["child"])
        self.assert_adds_up(recorder, 1)

    def test_a_parser_that_raises_on_a_line_never_stops_the_run_and_the_line_is_still_shown(self) -> None:
        backend = self.service.backends.get("draw-things-cli")

        class Broken:
            unterminated_redraws = False

            def readings(self, text: str, offset: float) -> tuple[Any, ...]:
                raise RuntimeError("parser bug")

            def accepts(self, text: str) -> bool:
                return False

        with mock.patch.object(type(backend), "stage_parser", return_value=Broken()):
            recorder = self.observe(self.job())
        outputs = [event for event in recorder.events if isinstance(event, RunOutput)]
        self.assertEqual(len(outputs), len(draw_things().lines))
        self.assertTrue(all(event.phase is None for event in outputs))
        self.assertEqual([event.status for event in recorder.events if isinstance(event, RunFinished)], ["succeeded"])

    def test_a_version_2_run_is_timed_under_the_backends_own_id_not_its_alias(self) -> None:
        # A version-2 file says `draw-things` for the backend the registry knows as `draw-things-cli`.
        job = load_job(self.write_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}])), self.global_config, self.params)
        self.assertEqual(job.run_backend(1), "draw-things")
        recorder = self.observe(job)
        (started,) = [event for event in recorder.events if isinstance(event, TimingStarted)]
        self.assertEqual((started.backend, started.pipeline), ("draw-things-cli", "i2v"))

    def test_the_description_of_a_run_before_it_starts_is_what_its_timing_records(self) -> None:
        job = self.job(run_count=2)
        recorder = self.observe(job)
        for started in [event for event in recorder.events if isinstance(event, TimingStarted)]:
            described = self.service.describe_run(job, started.number, executable="draw-things-cli")
            environment = {key: value for key, value in started.environment.items() if key != "load_start"}
            self.assertEqual((described.backend, described.pipeline, described.workload, described.environment), (started.backend, started.pipeline, started.workload, environment))
