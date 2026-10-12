"""Launch one backend command, classify its outcome, and finish a successful output."""

from __future__ import annotations

import time
from collections.abc import Callable

from loguru import logger

from draw_things_control.core.arguments import CommandArguments
from draw_things_control.core.errors import InputError
from draw_things_control.core.exit_codes import EXIT_TIMEOUT, exit_code_for_child_signal, exit_code_for_signal
from draw_things_control.core.generation import GenerationOutcome, require_executable
from draw_things_control.core.process.output import MessageCallback
from draw_things_control.core.process.runner import ChildStartCallback, JobRunnerFactory, RunResult, StoppableRunner
from draw_things_control.core.process.signals import CancelToken
from draw_things_control.jobs.backend import BackendRegistry
from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.jobs.events import RunStatus
from draw_things_control.jobs.media.checks import MediaCheck
from draw_things_control.jobs.media.toolkit import MediaTools
from draw_things_control.jobs.planning import PlannedRun
from draw_things_control.jobs.records import RunRecord


class RunLauncher:
    """Runs one planned run through the generation use case, under the job's cancel token."""

    def __init__(self, runner_factory: JobRunnerFactory[StoppableRunner], find_executable: Callable[[str], str | None], media: MediaTools, token: CancelToken, backends: BackendRegistry, report: Callable[[int, MediaCheck], None]) -> None:
        self._runner_factory = runner_factory
        self._find_executable = find_executable
        self._media = media
        self._token = token
        self._backends = backends
        self._report = report

    def launch(self, job: JobDefinition, run: PlannedRun, record: RunRecord, *, shutdown_grace: float, on_message: MessageCallback | None, on_start: ChildStartCallback | None, finish_state: object | None = None) -> tuple[RunStatus, int]:
        """Run ``run``, timing it into ``record``; return how it ended: its status and exit code."""
        started = time.monotonic()
        try:
            result = self._create_runner(run.arguments, job.run_timeout_seconds, shutdown_grace, on_message, on_start).run()
        finally:
            self._token.detach()
            record.seconds = round(time.monotonic() - started, 1)
        exit_code = self._exit_code(result)
        if result.timed_out:
            return RunStatus.TIMED_OUT, exit_code
        if result.termination_signal is not None:
            return RunStatus.INTERRUPTED, exit_code
        if exit_code != 0:
            return RunStatus.FAILED, exit_code
        backend = self._backends.get(job.backend)
        child_output = getattr(backend, "child_output", lambda planned: planned.output)(run)
        if not child_output.is_file():
            logger.error("{} exited with 0 but did not write {}", job.backend, child_output)
            return RunStatus.FAILED, 1
        failed = backend.finish(job, run, record, lambda: self._token.requested, finish_state, self._media, self._report)
        if failed is None and run.last_frame is not None and not run.last_frame.is_file():
            logger.error("{} did not produce the handoff {}", job.backend, run.last_frame)
            return RunStatus.FAILED, 1
        return failed if failed is not None else (RunStatus.SUCCEEDED, 0)

    @staticmethod
    def _exit_code(result: RunResult) -> int:
        if result.timed_out:
            return EXIT_TIMEOUT
        if result.termination_signal is not None:
            return exit_code_for_signal(result.termination_signal)
        if result.return_code < 0:
            return exit_code_for_child_signal(result.return_code)
        return result.return_code

    def generate(self, arguments: CommandArguments, *, timeout: float, shutdown_grace: float, on_start: ChildStartCallback | None = None) -> GenerationOutcome:
        """Run one bounded generation under the executor's cancellation token, without job planning or finishing."""
        if timeout <= 0:
            raise InputError("--timeout must be positive")
        if shutdown_grace < 0:
            raise InputError("--shutdown-grace must not be negative")
        require_executable(self._find_executable, arguments.command[0])
        try:
            result = self._create_runner(arguments, timeout, shutdown_grace, None, on_start).run()
            return GenerationOutcome(exit_code=self._exit_code(result), timed_out=result.timed_out, termination_signal=result.termination_signal)
        finally:
            self._token.detach()

    def _create_runner(self, arguments: CommandArguments, timeout: float | None, shutdown_grace: float, on_message: MessageCallback | None = None, on_start: ChildStartCallback | None = None, /) -> StoppableRunner:
        runner = self._runner_factory(arguments, timeout, shutdown_grace, on_message, on_start)
        self._token.attach(runner)
        return runner
