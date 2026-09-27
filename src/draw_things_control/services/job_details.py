"""What one job file says, read for a person: its summary and its dry-run plan."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.core.paths import ProjectPaths
from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.jobs.executor import JobExecutor
from draw_things_control.jobs.files import read_job
from draw_things_control.jobs.text import PLACEHOLDER_SEED, PlanStep, plan_header, plan_steps


@dataclass(frozen=True)
class JobDetails:
    """What /describe job prints: the job or its error, and the plan (its header lines, then each run) or why there is none."""

    job: JobDefinition | None
    error: str | None = None
    plan: tuple[str, ...] | None = None
    plan_error: str | None = None
    runs: tuple[PlanStep, ...] = ()


def error_text(path: Path, error: Exception) -> str:
    """The error without the job file's path in front, since the message already names the file."""
    message = str(error)
    prefix = f"{path.expanduser().resolve()}: "
    return message.removeprefix(prefix)


def read_details(path: Path, settings: GlobalConfig, paths: ProjectPaths) -> JobDetails:
    """Load the job, decoding its input, without its plan."""
    try:
        return JobDetails(read_job(path, settings, paths)[0])
    except (ValueError, OSError) as error:
        return JobDetails(None, error_text(path, error))


def add_plan(details: JobDetails, executor: JobExecutor, executable: str) -> JobDetails:
    """The details with the job's dry-run plan, using the placeholder seed when the job sets none."""
    assert details.job is not None
    try:
        preview = executor.preview(details.job, executable=executable, seed=PLACEHOLDER_SEED)
    except (ValueError, OSError) as error:
        return JobDetails(details.job, plan_error=str(error))
    job = details.job
    # The plan run-job --dry-run prints, from the same steps. The table leaves out the executable, so the header names it.
    return JobDetails(job, plan=(*plan_header(job, preview), f"Executable: {executable}"), runs=tuple(plan_steps(job, preview)))
