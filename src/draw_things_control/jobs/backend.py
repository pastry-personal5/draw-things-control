"""The boundary between a chained job and the generator that runs each step."""

from __future__ import annotations

import signal
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from draw_things_control.core.arguments import CommandArguments
from draw_things_control.core.errors import InputError
from draw_things_control.jobs.media.checks import MediaCheck
from draw_things_control.jobs.media.toolkit import MediaTools

if TYPE_CHECKING:
    from draw_things_control.jobs.definition import JobDefinition, PromptPair
    from draw_things_control.jobs.events import RunStatus
    from draw_things_control.jobs.parsing import JobParser
    from draw_things_control.jobs.planning import PlannedRun
    from draw_things_control.jobs.records import RunRecord


DRAW_THINGS_BACKEND = "draw-things-cli"


@dataclass(frozen=True)
class OutputPaths:
    """Backend-owned files created beside a run's main output."""

    handoff: Path | None = None
    raw_handoff: Path | None = None
    corrected_output: Path | None = None


@dataclass(frozen=True)
class RunPreparation:
    """Backend-owned finishing state and the anchor shown in run records."""

    finish_state: object | None = None
    record_anchor: Path | None = None
    chain_anchor: Path | None = None


@dataclass(frozen=True)
class RunContext:
    """Chain state available when a backend prepares one run's finishing work."""

    number: int
    pair: PromptPair
    input: Path | None
    first_image: Path | None
    chain_anchor: Path | None
    previous_pair: PromptPair | None
    notes: tuple[str, ...]
    fresh: bool


class JobBackend(Protocol):
    """One installed, process-based generator; shared chain code uses only this contract."""

    id: str

    @property
    def supported_modes(self) -> frozenset[str]: ...

    def parse_job(self, parser: JobParser, data: dict[str, Any], source_text: str) -> JobDefinition: ...

    def safe_settings(self, job: JobDefinition) -> dict[str, Any]:
        """Return serializable effective settings safe for records and front ends."""
        ...

    def snapshot_config(self, job: JobDefinition, params_directory: Path, max_bytes: int) -> str:
        """Capture any legacy base configuration the queued job must keep."""
        ...

    def finishing_seconds(self, job: JobDefinition) -> float: ...

    def check_tools(self, job: JobDefinition, executable: str, media: MediaTools, find_executable: Callable[[str], str | None]) -> None: ...

    def output_paths(self, job: JobDefinition, output: Path) -> OutputPaths: ...

    def arguments(self, job: JobDefinition, pair: PromptPair, run_input: Path | None, output: Path, seed: int, executable: str) -> CommandArguments: ...

    def prepare_run(self, job: JobDefinition, context: RunContext) -> RunPreparation: ...

    def redact(self, command: Sequence[str]) -> tuple[str, ...]: ...

    def finish(self, job: JobDefinition, run: PlannedRun, record: RunRecord, stop_requested: Callable[[], signal.Signals | None], finish_state: object | None, media: MediaTools, report: Callable[[int, MediaCheck], None] | None) -> tuple[RunStatus, int] | None: ...


class BackendRegistry:
    """Explicit installed backends; tests can inject a second adapter."""

    def __init__(self, backends: Sequence[JobBackend]) -> None:
        self._backends = {backend.id: backend for backend in backends}
        if len(self._backends) != len(backends):
            raise ValueError("Backend IDs must be unique")

    def get(self, backend_id: str) -> JobBackend:
        try:
            return self._backends[backend_id]
        except KeyError as error:
            raise InputError(f"Unknown backend '{backend_id}'", field="backend") from error

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._backends)
