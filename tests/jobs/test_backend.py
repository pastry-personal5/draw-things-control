"""A second adapter proves that the shared chain does not require Draw Things settings."""

from __future__ import annotations

import json
import signal
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from framecontrol.core.errors import InputError
from framecontrol.jobs.backend import BackendRegistry, OutputPaths, RunContext, RunPreparation
from framecontrol.jobs.backends import DrawThingsBackend
from framecontrol.jobs.definition import GenerationMode, JobDefinition
from framecontrol.jobs.events import RunStatus
from framecontrol.jobs.executor import JobExecutor, JobRunOptions, ResumePoint
from framecontrol.jobs.media.toolkit import MediaTools
from framecontrol.jobs.parsing import JobParser, load_job
from framecontrol.services.api_rules import check_api_rules
from framecontrol.services.queue_submit import parse_snapshot, submit_job
from framecontrol.state.store import Store, StoreMode
from tests.fixtures import JobTestCase, job_data


@dataclass(frozen=True)
class FakeSettings:
    label: str


@dataclass(frozen=True)
class FakeCommand:
    image: Path | None
    output: Path
    seed: int
    label: str

    @property
    def command(self) -> tuple[str, ...]:
        return ("fake-generator", "--image", str(self.image), "--output", str(self.output), "--seed", str(self.seed), "--label", self.label)


class FakeBackend:
    id = "fake"
    supported_modes = frozenset({"i2i"})

    def finishing_seconds(self, job: JobDefinition) -> float:
        return 0.0

    def estimated_output_bytes(self, job: JobDefinition, runs: int) -> int:
        return 0

    def parse_job(self, parser: JobParser, data: dict[str, Any], source_text: str) -> JobDefinition:
        common = parser.parse_common(data)
        if common.mode != GenerationMode.I2I:
            parser.fail("mode", "is not supported by fake")
        for key in ("config_file", "config_override", "color"):
            if key in data:
                parser.fail(key, "is not supported by fake")
        settings = data.get("backend_settings")
        if not isinstance(settings, dict) or set(settings) != {"label"} or not isinstance(settings["label"], str):
            parser.fail("backend_settings.label", "must be text")
        output = data.get("output", {})
        directory = parser.output_directory(output, common.name, {"directory", "extension"})
        extension = output.get("extension", "png")
        if extension != "png":
            parser.fail("output.extension", "must be png for fake")
        return JobDefinition(path=parser.path, name=common.name, mode=common.mode, input=common.input, run_count=common.run_count, prompt_pairs=common.prompt_pairs, output_directory=directory, extension=extension, run_timeout_seconds=common.run_timeout_seconds, cooldown=common.cooldown, cooldown_source=common.cooldown_source, backend=self.id, backend_settings={"label": settings["label"]}, backend_data=FakeSettings(settings["label"]), source_text=source_text)

    def safe_settings(self, job: JobDefinition) -> dict[str, Any]:
        return {"label": "[redacted]"}

    def snapshot_config(self, job: JobDefinition, params_directory: Path, max_bytes: int) -> str:
        return ""

    def check_tools(self, job: JobDefinition, executable: str, media: MediaTools, find_executable: Any) -> None:
        if find_executable("fake-generator") is None:
            raise InputError("fake-generator is missing")

    def output_paths(self, job: JobDefinition, output: Path) -> OutputPaths:
        return OutputPaths()

    def arguments(self, job: JobDefinition, pair: Any, run_input: Path | None, output: Path, seed: int, executable: str, run_number: int = 1) -> FakeCommand:
        assert isinstance(job.backend_data, FakeSettings)
        return FakeCommand(run_input, output, seed, job.backend_data.label)

    def prepare_run(self, job: JobDefinition, context: RunContext) -> RunPreparation:
        return RunPreparation(chain_anchor=context.chain_anchor)

    def redact(self, command: Any) -> tuple[str, ...]:
        values = tuple(command)
        index = values.index("--label")
        return (*values[: index + 1], "[redacted]")

    def finish(self, job: JobDefinition, run: Any, record: Any, stop_requested: Any, finish_state: object | None, media: MediaTools, report: Any) -> tuple[RunStatus, int] | None:
        return None if run.output.stat().st_size else (RunStatus.FAILED, 1)


@dataclass(frozen=True)
class FakeResult:
    return_code: int = 0
    timed_out: bool = False
    termination_signal: signal.Signals | None = None


class FakeRunner:
    def __init__(self, arguments: FakeCommand, result: FakeResult) -> None:
        self.arguments = arguments
        self.result = result

    def run(self) -> FakeResult:
        if self.result.return_code == 0 and not self.result.timed_out and self.result.termination_signal is None:
            self.arguments.output.write_bytes(b"complete")
        return self.result

    def request_shutdown(self, received_signal: signal.Signals = signal.SIGTERM) -> None:
        self.result = FakeResult(termination_signal=received_signal)


class BackendContractTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.backends = BackendRegistry((DrawThingsBackend(), FakeBackend()))
        self.job_path = self.write_job(job_data(backend="fake", backend_settings={"label": "sentinel-secret-label"}, config_file=None, mode="i2i", run_count=3, prompt_pairs=[{"name": "only", "positive": "walk"}]))

    def _job(self) -> JobDefinition:
        return load_job(self.job_path, self.global_config, self.params, backends=self.backends)

    def _executor(self, results: list[FakeResult], seen: list[FakeCommand]) -> JobExecutor:
        def runner(arguments: FakeCommand, *_rest: Any) -> FakeRunner:
            seen.append(arguments)
            return FakeRunner(arguments, results.pop(0))

        return JobExecutor(runner, lambda name: name, MediaTools(require_ffmpeg=lambda: "ffmpeg", frame_extractor=lambda *_args: None), random_seed=lambda: 7, handle_signals=False, backends=self.backends)

    def test_fake_backend_parses_without_draw_things_configuration_and_previews_redacted_commands(self) -> None:
        job = self._job()
        self.assertEqual((job.backend, job.config_file, job.backend_data), ("fake", "", FakeSettings("sentinel-secret-label")))
        self.assertEqual(job.backend_settings, {"label": "[redacted]"})
        preview = self._executor([], []).preview(job, executable="draw-things-cli", seed=7)
        self.assertEqual(len(preview.runs), 3)
        self.assertIn("[redacted]", preview.commands[0])
        self.assertNotIn("sentinel-secret-label", preview.command_previews[0])

    def test_fake_backend_records_only_safe_settings_and_commands(self) -> None:
        from framecontrol.jobs.events import event_to_dict

        events: list[Any] = []
        outcome = self._executor([FakeResult(), FakeResult(), FakeResult()], []).run(self._job(), JobRunOptions(executable="draw-things-cli", shutdown_grace=1, write_records=True, observer=events.append))
        assert outcome.manifest is not None
        manifest = json.loads(outcome.manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest["backend_settings"], {"label": "[redacted]"})
        public_events = [event_to_dict(event) for event in events]
        self.assertTrue(all(event.get("backend_settings") == {"label": "[redacted]"} for event in public_events if event["kind"] == "job_started"))
        self.assertNotIn("sentinel-secret-label", json.dumps({"manifest": manifest, "commands": [event["command"] for event in public_events if event["kind"] == "run_started"]}))

    def test_fake_backend_queue_snapshot_keeps_its_validated_settings(self) -> None:
        store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        self.addCleanup(store.close)
        entry = submit_job(self.job_path, self.global_config, self.params, store, backends=self.backends)
        self.assertEqual((entry.backend, entry.settings.backend_settings), ("fake", {"label": "[redacted]"}))
        self.job_path.write_text("invalid: replacement\n", encoding="utf-8")
        restored = parse_snapshot(entry, self.global_config, self.params, backends=self.backends)()
        self.assertEqual((restored.backend, restored.backend_data, restored.backend_settings), ("fake", FakeSettings("sentinel-secret-label"), {"label": "[redacted]"}))

    def test_fake_backend_runs_and_resumes_from_its_own_handoff(self) -> None:
        job = self._job()
        seen: list[FakeCommand] = []
        first = self._executor([FakeResult(), FakeResult(return_code=2)], seen)
        outcome = first.run(job, JobRunOptions(executable="draw-things-cli", shutdown_grace=1))
        self.assertEqual((outcome.exit_code, outcome.completed_runs), (2, 1))
        self.assertEqual(seen[1].image, seen[0].output)
        second = self._executor([FakeResult(), FakeResult()], seen)
        resumed = second.run(job, JobRunOptions(executable="draw-things-cli", shutdown_grace=1, resume=ResumePoint(first_run=2, input=seen[0].output, seed=7)))
        self.assertEqual((resumed.exit_code, resumed.completed_runs), (0, 2))
        self.assertEqual((seen[2].image, seen[2].seed, seen[3].image), (seen[0].output, 7, seen[2].output))

    def test_fake_backend_timeout_and_wrong_fields(self) -> None:
        job = self._job()
        timed_out = self._executor([FakeResult(timed_out=True)], []).run(job, JobRunOptions(executable="draw-things-cli", shutdown_grace=1))
        self.assertEqual(timed_out.exit_code, 124)
        for field in ("config_file", "config_override", "color"):
            changed = job_data(backend="fake", backend_settings={"label": "sentinel-secret-label"}, config_file=None, mode="i2i")
            changed[field] = "wrong"
            with self.subTest(field=field), self.assertRaises(InputError) as caught:
                load_job(self.write_job(changed), self.global_config, self.params, backends=self.backends)
            self.assertEqual(caught.exception.field, field)

    def test_backend_owned_mode_reaches_the_adapter(self) -> None:
        data = job_data(backend="draw-things-cli", mode="i2av", prompt_pairs=[{"name": "only", "positive": "walk"}])
        with self.assertRaises(InputError) as caught:
            load_job(self.write_job(data), self.global_config, self.params, backends=self.backends)
        self.assertEqual(caught.exception.field, "mode")
        self.assertIn("is not supported by draw-things-cli", str(caught.exception))

    def test_api_rules_use_the_supplied_backend_registry(self) -> None:
        job = replace(self._job(), run_timeout_seconds=1)
        check_api_rules(job, self.global_config, self.global_config.api_limits, queued_count=0, backends=self.backends)

    def test_fake_backend_cancels_the_supervised_child(self) -> None:
        started = threading.Event()
        stopped = threading.Event()
        outcomes: list[Any] = []

        class BlockingRunner(FakeRunner):
            def run(self) -> FakeResult:
                started.set()
                if not stopped.wait(3):
                    raise AssertionError("fake child was not cancelled")
                return self.result

            def request_shutdown(self, received_signal: signal.Signals = signal.SIGTERM) -> None:
                super().request_shutdown(received_signal)
                stopped.set()

        executor = JobExecutor(lambda arguments, *_rest: BlockingRunner(arguments, FakeResult()), lambda name: name, MediaTools(require_ffmpeg=lambda: "ffmpeg", frame_extractor=lambda *_args: None), random_seed=lambda: 7, handle_signals=False, backends=self.backends)
        thread = threading.Thread(target=lambda: outcomes.append(executor.run(self._job(), JobRunOptions(executable="draw-things-cli", shutdown_grace=1))))
        thread.start()
        self.assertTrue(started.wait(3))
        self.assertTrue(executor.cancel(signal.SIGTERM))
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes[0].exit_code, 143)
