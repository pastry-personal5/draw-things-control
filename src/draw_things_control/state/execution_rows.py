"""Executions and their runs: the rows as plain values, read and written by ``ExecutionRepository``."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from draw_things_control.jobs.events import JobStatus, RunStatus
from draw_things_control.state.ids import execution_id_text


def epoch(timestamp: str) -> float:
    """The UTC epoch of a local ISO 8601 timestamp with an offset, for ordering and retention."""
    return datetime.fromisoformat(timestamp).timestamp()


@dataclass(frozen=True)
class ExecutionSettings:
    """What an execution ran with, besides its columns: kept as JSON, so every key is optional (an imported execution has fewer)."""

    input: str | None = None
    output_directory: str | None = None
    cooldown_seconds: float | None = None
    cooldown_source: str | None = None
    # The resolved cooldown mapping; absent in an execution written before it was kept, which had only ``cooldown_seconds``.
    cooldown: dict[str, Any] | None = None
    config_file: str | None = None
    config_override: dict[str, Any] | None = None
    input_resize: dict[str, Any] | None = None

    def to_json(self) -> str:
        """The keys that are set, as JSON."""
        return json.dumps({name: value for name, value in self.__dict__.items() if value is not None}, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> ExecutionSettings:
        data = json.loads(text) if text else {}
        return cls(**{name: data.get(name) for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class NewRun:
    """A run to insert: its prompts, command (credentials redacted), files, and, when it is already over, its result."""

    pair: str
    positive: str
    started_at: str
    negative: str | None = None
    input: str | None = None
    resized_input: str | None = None
    output: str | None = None
    last_frame: str | None = None
    command: Sequence[str] = ()
    status: str = RunStatus.RUNNING
    seconds: float | None = None
    exit_code: int | None = None
    cooldown_after_seconds: float | None = None
    output_width: int | None = None
    output_height: int | None = None
    output_frames: int | None = None


@dataclass(frozen=True)
class NewExecution:
    """An execution to insert. Without an ``execution_number`` (one ``ExecutionRepository.reserve_number`` gave), the next one is taken."""

    job_name: str
    job_file: str
    mode: str
    started_at: str
    status: str = JobStatus.RUNNING
    model: str | None = None
    seed: int | None = None
    seed_source: str | None = None
    cooldown_seconds: float | None = None
    cooldown_source: str | None = None
    total_runs: int | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    signal: str | None = None
    manifest_path: str | None = None
    log_path: str | None = None
    config_file: str | None = None
    job_yaml: str | None = None
    recovered_at: str | None = None
    settings: ExecutionSettings = field(default_factory=ExecutionSettings)
    execution_number: int | None = None
    # The first run's number (above 1 for a resume) and, for a resume, the execution number it resumes; the queue's
    # own snapshot names the entry, not the execution, so this is the only place a resumed execution is linked.
    first_run: int = 1
    resumes: int | None = None


@dataclass(frozen=True)
class RunRow:
    """One stored run of an execution."""

    number: int
    pair: str
    positive: str
    negative: str | None
    input: str | None
    resized_input: str | None
    output: str | None
    last_frame: str | None
    command: tuple[str, ...]
    started_at: str
    seconds: float | None
    exit_code: int | None
    status: str
    cooldown_after_seconds: float | None
    output_width: int | None
    output_height: int | None
    output_frames: int | None
    # The store's own row number and start time as an epoch, for ordering.
    id: int = 0
    started_epoch: float = 0.0

    @classmethod
    def from_row(cls, row: sqlite3.Row, running_as_interrupted: bool = False) -> RunRow:
        status = row["status"]
        return cls(
            number=row["number"],
            pair=row["pair"],
            positive=row["positive"],
            negative=row["negative"],
            input=row["input"],
            resized_input=row["resized_input"],
            output=row["output"],
            last_frame=row["last_frame"],
            command=tuple(json.loads(row["command"])),
            started_at=row["started_at"],
            seconds=row["seconds"],
            exit_code=row["exit_code"],
            status=RunStatus.INTERRUPTED if running_as_interrupted and status == RunStatus.RUNNING else status,
            cooldown_after_seconds=row["cooldown_after_seconds"],
            output_width=row["output_width"],
            output_height=row["output_height"],
            output_frames=row["output_frames"],
            id=row["id"],
            started_epoch=row["started_epoch"],
        )


@dataclass(frozen=True)
class ExecutionRow:
    """One stored execution. ``runs`` is filled only when it is read by itself; ``succeeded`` counts its successful runs."""

    # The store's own row number, which stays internal: a person sees ``label``.
    id: int
    execution_number: int
    job_name: str
    job_file: str
    mode: str
    status: str
    model: str | None
    seed: int | None
    seed_source: str | None
    cooldown_seconds: float | None
    cooldown_source: str | None
    total_runs: int | None
    started_at: str
    started_epoch: float
    finished_at: str | None
    finished_epoch: float | None
    exit_code: int | None
    signal: str | None
    manifest_path: str | None
    log_path: str | None
    config_file: str | None
    job_yaml: str | None
    settings: ExecutionSettings
    recovered_at: str | None
    succeeded: int = 0
    runs: tuple[RunRow, ...] = ()
    first_run: int = 1
    resumes: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row, running_as_interrupted: bool = False, runs: tuple[RunRow, ...] = ()) -> ExecutionRow:
        status = row["status"]
        return cls(
            id=row["id"],
            execution_number=row["execution_number"],
            job_name=row["job_name"],
            job_file=row["job_file"],
            mode=row["mode"],
            status=JobStatus.INTERRUPTED if running_as_interrupted and status == JobStatus.RUNNING else status,
            model=row["model"],
            seed=row["seed"],
            seed_source=row["seed_source"],
            cooldown_seconds=row["cooldown_seconds"],
            cooldown_source=row["cooldown_source"],
            total_runs=row["total_runs"],
            started_at=row["started_at"],
            started_epoch=row["started_epoch"],
            finished_at=row["finished_at"],
            finished_epoch=row["finished_epoch"],
            exit_code=row["exit_code"],
            signal=row["signal"],
            manifest_path=row["manifest_path"],
            log_path=row["log_path"],
            config_file=row["config_file"],
            job_yaml=row["job_yaml"],
            settings=ExecutionSettings.from_json(row["settings"]),
            recovered_at=row["recovered_at"],
            succeeded=row["succeeded"] if "succeeded" in row.keys() else 0,
            runs=runs,
            first_run=row["first_run"] if "first_run" in row.keys() and row["first_run"] is not None else 1,
            resumes=row["resumes"] if "resumes" in row.keys() else None,
        )

    @property
    def label(self) -> str:
        """The execution's ID as people see it (E0012)."""
        return execution_id_text(self.execution_number)

    @property
    def imported(self) -> bool:
        """Imported phase 1 executions have no job snapshot."""
        return self.job_yaml is None

    @property
    def output_directory(self) -> Path | None:
        """Where the execution's outputs are: as recorded, or beside its manifest for an imported one."""
        if self.settings.output_directory:
            return Path(self.settings.output_directory)
        return Path(self.manifest_path).parent if self.manifest_path else None

    def run_file(self, name: str | None) -> Path | None:
        """The full path of a run's output or last frame, which the store keeps as a file name."""
        if not name:
            return None
        if Path(name).is_absolute():
            return Path(name)
        directory = self.output_directory
        return directory / name if directory is not None else None

    @property
    def succeeded_runs(self) -> tuple[RunRow, ...]:
        """The runs that finished successfully, in run order."""
        return tuple(run for run in self.runs if run.status == RunStatus.SUCCEEDED)

    def run(self, number: int) -> RunRow | None:
        return next((run for run in self.runs if run.number == number), None)


EXECUTION_COLUMNS = ("execution_number", "job_name", "job_file", "mode", "status", "model", "seed", "seed_source", "cooldown_seconds", "cooldown_source", "total_runs", "started_at", "finished_at", "exit_code", "signal", "manifest_path", "log_path", "config_file", "job_yaml", "recovered_at", "first_run", "resumes")
RUN_COLUMNS = ("pair", "positive", "negative", "input", "resized_input", "output", "last_frame", "started_at", "seconds", "exit_code", "status", "cooldown_after_seconds", "output_width", "output_height", "output_frames")
# Each execution's count of successful runs, read with it.
SUCCEEDED_COUNT = "(SELECT COUNT(*) FROM runs WHERE runs.execution_id = executions.id AND runs.status = 'succeeded') AS succeeded"
