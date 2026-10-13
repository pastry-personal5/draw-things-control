"""The rules and limits every job the HTTP API runs or writes must meet (Milestone 02), checked at every submission
and resume, and (Milestone 07) at every write, so a job an agent can write is a job it can run. Whoever calls, ``fc
queue`` included, and with ``--allow-write`` on too: the API cannot tell a person from an agent (owner decision)."""

from __future__ import annotations

from framechain.core.cooldown import CooldownPolicy
from framechain.core.errors import LimitExceededError, TimeoutRequiredError
from framechain.core.global_config import ApiLimits, GlobalConfig
from framechain.jobs.backend import BackendRegistry
from framechain.jobs.backends import DEFAULT_BACKENDS
from framechain.jobs.definition import JobDefinition


def check_job_rules(job: JobDefinition, global_config: GlobalConfig) -> None:
    """Require a finite timeout; the parser checks path containment before touching either path."""
    if job.run_timeout_seconds is None:
        raise TimeoutRequiredError("'run_timeout_seconds' is required for a job the API runs or writes", field="run_timeout_seconds")


def check_job_limits(job: JobDefinition, limits: ApiLimits, *, remaining_runs: int | None = None, backends: BackendRegistry = DEFAULT_BACKENDS) -> None:
    """Check run count, worst-case duration, and estimated H3 output storage. Duration includes each backend's
    finishing budget and the longest cooldown between runs. A job exactly at a limit is accepted.
    ``remaining_runs``, for a resume, is the runs it has left, not the whole chain's (owner decision)."""
    runs = remaining_runs if remaining_runs is not None else job.run_count
    if runs > limits.max_job_runs:
        raise LimitExceededError(f"{runs} runs is over the max_job_runs limit of {limits.max_job_runs}", key="max_job_runs", limit=limits.max_job_runs, value=runs)
    assert job.run_timeout_seconds is not None  # check_job_rules already required it
    worst_case = _worst_case_seconds(job.run_timeout_seconds, job.cooldown, runs) + runs * backends.get(job.backend).finishing_seconds(job)
    if worst_case > limits.max_job_seconds:
        raise LimitExceededError(f"a worst case of {worst_case:g} seconds is over the max_job_seconds limit of {limits.max_job_seconds:g}", key="max_job_seconds", limit=limits.max_job_seconds, value=worst_case)
    estimated_output = backends.get(job.backend).estimated_output_bytes(job, runs)
    if estimated_output > limits.max_job_output_bytes:
        raise LimitExceededError(f"an estimated output of {estimated_output} bytes is over the max_job_output_bytes limit of {limits.max_job_output_bytes}", key="max_job_output_bytes", limit=limits.max_job_output_bytes, value=estimated_output)


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


def check_api_rules(job: JobDefinition, global_config: GlobalConfig, limits: ApiLimits, *, queued_count: int, remaining_runs: int | None = None, backends: BackendRegistry = DEFAULT_BACKENDS) -> None:
    """Every rule and limit a submission or resume must meet, in the order the acceptance criteria check them:
    the structural rules, the queue-length limit, then the job's own run and time limits."""
    check_job_rules(job, global_config)
    check_queue_not_full(queued_count, limits)
    check_job_limits(job, limits, remaining_runs=remaining_runs, backends=backends)
