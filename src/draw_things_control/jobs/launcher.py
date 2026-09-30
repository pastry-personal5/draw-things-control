"""Launch one run of a job: start draw-things-cli, classify how it ended, and finish a successful output."""

from __future__ import annotations

import time
from collections.abc import Callable

from loguru import logger

from draw_things_control.core.arguments import DrawThingsGenerateArguments
from draw_things_control.core.draw_things_config import load_config
from draw_things_control.core.generation import GenerationService
from draw_things_control.core.process.output import MessageCallback
from draw_things_control.core.process.runner import ChildStartCallback, RunnerFactory, StoppableRunner
from draw_things_control.core.process.signals import CancelToken
from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.jobs.events import RunStatus
from draw_things_control.jobs.planning import PlannedRun
from draw_things_control.jobs.records import RunRecord
from draw_things_control.jobs.run_finisher import RunColor, RunFinisher


class RunLauncher:
    """Runs one planned run through the generation use case, under the job's cancel token."""

    def __init__(self, runner_factory: RunnerFactory[StoppableRunner], find_executable: Callable[[str], str | None], finisher: RunFinisher, token: CancelToken) -> None:
        self._runner_factory = runner_factory
        self._finisher = finisher
        self._token = token
        self._generation = GenerationService(runner_factory=self._create_runner, find_executable=find_executable, config_loader=load_config)

    def launch(self, job: JobDefinition, run: PlannedRun, record: RunRecord, *, shutdown_grace: float, on_message: MessageCallback | None, on_start: ChildStartCallback | None, color: RunColor | None = None) -> tuple[RunStatus, int]:
        """Run ``run``, timing it into ``record``; return how it ended: its status and exit code."""
        started = time.monotonic()
        try:
            outcome = self._generation.execute(run.arguments, dry_run=False, timeout=job.run_timeout_seconds, shutdown_grace=shutdown_grace, on_message=on_message, on_start=on_start)
        finally:
            self._token.detach()
            record.seconds = round(time.monotonic() - started, 1)
        if outcome.timed_out:
            return RunStatus.TIMED_OUT, outcome.exit_code
        if outcome.termination_signal is not None:
            return RunStatus.INTERRUPTED, outcome.exit_code
        if outcome.exit_code != 0:
            return RunStatus.FAILED, outcome.exit_code
        if not run.output.is_file():
            logger.error("draw-things-cli exited with 0 but did not write {}", run.output)
            return RunStatus.FAILED, 1
        failed = self._finisher.finish(job, run, record, lambda: self._token.requested, color)
        return failed if failed is not None else (RunStatus.SUCCEEDED, 0)

    def _create_runner(self, arguments: DrawThingsGenerateArguments, timeout: float | None, shutdown_grace: float, on_message: MessageCallback | None = None, on_start: ChildStartCallback | None = None, /) -> StoppableRunner:
        runner = self._runner_factory(arguments, timeout, shutdown_grace, on_message, on_start)
        self._token.attach(runner)
        return runner
