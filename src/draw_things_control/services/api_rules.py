"""The rules and limits every job the HTTP API runs or writes must meet (Milestone 02), checked at every submission
and resume, and (Milestone 07) at every write, so a job an agent can write is a job it can run. Whoever calls, ``dtc
queue`` included, and with ``--allow-write`` on too: the API cannot tell a person from an agent (owner decision)."""

from __future__ import annotations

from draw_things_control.core.cooldown import CooldownPolicy
from draw_things_control.core.errors import LimitExceededError, OutsideDirectoryError, TimeoutRequiredError
from draw_things_control.core.global_config import ApiLimits, GlobalConfig
from draw_things_control.jobs.definition import JobDefinition


def check_job_rules(job: JobDefinition, global_config: GlobalConfig) -> None:
    """The two structural rules: ``run_timeout_seconds`` is set, since without it the worst case is unbounded, and
    the job's ``input`` (symbolic links resolved) stays inside the input directory and its ``output.directory``
    inside the global output directory, so a job cannot create directories, or be told to read a file, anywhere
    else. Since Milestone 03 retired every way to run a job but the queue, a job file that breaks either no longer
    runs at all: every submission, from a person or an agent alike, passes through here first."""
    if job.run_timeout_seconds is None:
        raise TimeoutRequiredError("'run_timeout_seconds' is required for a job the API runs or writes", field="run_timeout_seconds")
    if job.input is not None:
        # Parsing already resolved job.input (jobs/parsing.py's _input_path), following any symbolic link; the
        # configured directory is resolved here too, in case it is itself a symbolic link.
        input_directory = global_config.input_directory.resolve()
        if not job.input.is_relative_to(input_directory):
            raise OutsideDirectoryError(f"'input' must be inside {input_directory}: {job.input}", field="input")
    output_directory = global_config.output_directory.resolve()
    resolved_output = job.output_directory.resolve()
    if not resolved_output.is_relative_to(output_directory):
        raise OutsideDirectoryError(f"'output.directory' must be inside {output_directory}: {resolved_output}", field="output.directory")


def check_job_limits(job: JobDefinition, limits: ApiLimits, *, remaining_runs: int | None = None) -> None:
    """``max_job_runs`` and ``max_job_seconds``, the job's worst case: its runs times ``run_timeout_seconds`` and, for a
    job that corrects its colors, the correction's time limit (Milestone 09), plus the longest wait between runs (the
    job's cooldown applied to ``run_timeout_seconds``, since a wait follows only a run that succeeded within it) times
    one less than its runs. A job exactly at a limit is accepted.
    ``remaining_runs``, for a resume, is the runs it has left, not the whole chain's (owner decision)."""
    runs = remaining_runs if remaining_runs is not None else job.run_count
    if runs > limits.max_job_runs:
        raise LimitExceededError(f"{runs} runs is over the max_job_runs limit of {limits.max_job_runs}", key="max_job_runs", limit=limits.max_job_runs, value=runs)
    assert job.run_timeout_seconds is not None  # check_job_rules already required it
    worst_case = _worst_case_seconds(job.run_timeout_seconds, job.cooldown, runs) + runs * job.correction_seconds()
    if worst_case > limits.max_job_seconds:
        raise LimitExceededError(f"a worst case of {worst_case:g} seconds is over the max_job_seconds limit of {limits.max_job_seconds:g}", key="max_job_seconds", limit=limits.max_job_seconds, value=worst_case)


def _worst_case_seconds(run_timeout_seconds: float, cooldown: CooldownPolicy, runs: int) -> float:
    if runs <= 0:
        return 0.0
    wait = cooldown.wait_after(run_timeout_seconds).seconds
    return runs * run_timeout_seconds + (runs - 1) * wait


def check_queue_not_full(queued_count: int, limits: ApiLimits) -> None:
    """``max_queued_jobs``: entries ``queued`` at once. Checked separately from ``check_job_limits`` since it needs
    the store's current count, not anything about the job itself."""
    if queued_count >= limits.max_queued_jobs:
        raise LimitExceededError(f"{queued_count} entries are already queued, at the max_queued_jobs limit of {limits.max_queued_jobs}", key="max_queued_jobs", limit=limits.max_queued_jobs, value=queued_count)


def check_api_rules(job: JobDefinition, global_config: GlobalConfig, limits: ApiLimits, *, queued_count: int, remaining_runs: int | None = None) -> None:
    """Every rule and limit a submission or resume must meet, in the order the acceptance criteria check them:
    the structural rules, the queue-length limit, then the job's own run and time limits."""
    check_job_rules(job, global_config)
    check_queue_not_full(queued_count, limits)
    check_job_limits(job, limits, remaining_runs=remaining_runs)
