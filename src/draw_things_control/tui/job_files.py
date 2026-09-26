"""Reading the job files in the data directory for the TUI: pure functions, no widgets, so they run on worker threads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.jobs.job_definition import JobDefinition
from draw_things_control.jobs.job_report import PLACEHOLDER_SEED, job_files, plan_lines, read_job
from draw_things_control.jobs.job_service import JobService


@dataclass(frozen=True)
class JobRow:
    """One job file: the job, or why it is invalid."""

    path: Path
    job: JobDefinition | None
    error: str | None = None


@dataclass(frozen=True)
class JobDetails:
    """What /describe job prints: the job or its error, and the plan or why there is none."""

    job: JobDefinition | None
    error: str | None = None
    plan: tuple[str, ...] | None = None
    plan_error: str | None = None


def error_text(path: Path, error: Exception) -> str:
    """The error without the job file's path in front, since the message already names the file."""
    message = str(error)
    prefix = f"{path.expanduser().resolve()}: "
    return message.removeprefix(prefix)


def read_rows(directory: Path, settings: GlobalConfig) -> tuple[list[JobRow], str | None]:
    """Validate every job file without decoding inputs; return the rows, and a message when there are none."""
    if not directory.is_dir():
        return [], f"Data directory not found: {directory}"
    try:
        paths = job_files(directory)
    except OSError as error:
        return [], f"Cannot read the data directory {directory}: {error.strerror}"
    rows: list[JobRow] = []
    for path in paths:
        try:
            rows.append(JobRow(path, read_job(path, settings, decode_input=False)[0]))
        except (ValueError, OSError) as error:
            rows.append(JobRow(path, None, error_text(path, error)))
    return rows, None if rows else f"No job files (*.yaml, *.yml) in {directory}"


def read_details(path: Path, settings: GlobalConfig) -> JobDetails:
    """Load the job, decoding its input, without its plan."""
    try:
        return JobDetails(read_job(path, settings)[0])
    except (ValueError, OSError) as error:
        return JobDetails(None, error_text(path, error))


def add_plan(details: JobDetails, service: JobService, executable: str) -> JobDetails:
    """The details with the job's dry-run plan, using the placeholder seed when the job sets none."""
    assert details.job is not None
    try:
        preview = service.preview(details.job, executable=executable, seed=PLACEHOLDER_SEED)
    except (ValueError, OSError) as error:
        return JobDetails(details.job, plan_error=str(error))
    return JobDetails(details.job, plan=tuple(plan_lines(details.job, preview)))


def find_job(directory: Path, name: str) -> Path | str:
    """The job file named ``name`` in the data directory, or with that name before its suffix when only one has it; else why not."""
    try:
        paths = job_files(directory) if directory.is_dir() else []
    except OSError as error:
        return f"Cannot read the data directory {directory}: {error.strerror}"
    for path in paths:
        if path.name == name:
            return path
    matches = [path for path in paths if path.stem == name]
    if len(matches) == 1:
        return matches[0]
    if matches:
        return f"'{name}' matches {', '.join(path.name for path in matches)}; give the file name"
    return f"No job file '{name}' in {directory}"
