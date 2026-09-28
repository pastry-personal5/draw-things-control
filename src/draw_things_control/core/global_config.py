"""Load the machine-specific global configuration for jobs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from draw_things_control.core.cooldown import CooldownPolicy, parse_cooldown, replaced_cooldown_message
from draw_things_control.core.numbers import is_int, is_number
from draw_things_control.core.paths import EXAMPLE_GLOBAL_CONFIG_RELATIVE, GLOBAL_CONFIG_RELATIVE
from draw_things_control.core.yaml_files import read_yaml_file

GLOBAL_CONFIG_KEYS = {"version", "input_directory", "output_directory", "write_job_records", "cooldown", "history_retention_days", "api_limits"}
DEFAULT_HISTORY_RETENTION_DAYS = 14
MAX_HISTORY_RETENTION_DAYS = 3650
REQUIRED_GLOBAL_CONFIG_KEYS = ("input_directory", "output_directory", "version")

API_LIMITS_KEYS = {"max_queued_jobs", "max_job_runs", "max_job_seconds", "max_job_file_bytes"}
DEFAULT_MAX_QUEUED_JOBS = 20
DEFAULT_MAX_JOB_RUNS = 100
DEFAULT_MAX_JOB_SECONDS = 172800
DEFAULT_MAX_JOB_FILE_BYTES = 65536


@dataclass(frozen=True)
class ApiLimits:
    """Bounded work the HTTP API enforces from its first submission (Milestone 02): entries queued at once, runs a
    submitted job (or a resume's remaining runs) may have, one entry's worst-case seconds, and (Milestone 07) the
    size of job text the API accepts. Whoever calls, `dtc queue` included, and with `--allow-write` on too."""

    max_queued_jobs: int = DEFAULT_MAX_QUEUED_JOBS
    max_job_runs: int = DEFAULT_MAX_JOB_RUNS
    max_job_seconds: float = DEFAULT_MAX_JOB_SECONDS
    max_job_file_bytes: int = DEFAULT_MAX_JOB_FILE_BYTES


DEFAULT_API_LIMITS = ApiLimits()


@dataclass(frozen=True)
class GlobalConfig:
    """Default directories for job inputs and outputs, and machine-wide job defaults."""

    input_directory: Path
    output_directory: Path
    write_job_records: bool = False
    # The wait between runs, unless a job sets its own cooldown; None when the file sets none.
    cooldown: CooldownPolicy | None = None
    # Execution history older than this many days is pruned from the state store; 0 keeps it forever.
    history_retention_days: int = DEFAULT_HISTORY_RETENTION_DAYS
    # The HTTP API's limits (Milestone 02); unused outside the server.
    api_limits: ApiLimits = DEFAULT_API_LIMITS


def load_global_config(path: Path, *, is_default: bool = False) -> GlobalConfig:
    """Parse and validate the global configuration file; ``is_default`` says it is the project's own, whose absence the message explains how to fix."""
    if not path.exists():
        hint = f"; copy {EXAMPLE_GLOBAL_CONFIG_RELATIVE} to {GLOBAL_CONFIG_RELATIVE} and edit its paths" if is_default else ""
        raise ValueError(f"Global configuration not found: {path}{hint}")
    data = read_yaml_file(path, "Global configuration", show_source=True)[0]
    if "cooldown_seconds" in data:
        raise ValueError(f"{path}: {replaced_cooldown_message(data['cooldown_seconds'])}")
    unknown = sorted(set(data) - GLOBAL_CONFIG_KEYS)
    if unknown:
        raise ValueError(f"{path}: unknown key '{unknown[0]}'")
    for key in REQUIRED_GLOBAL_CONFIG_KEYS:
        if key not in data:
            raise ValueError(f"{path}: '{key}' is required")
    if data["version"] != 1 or isinstance(data["version"], bool):
        raise ValueError(f"{path}: 'version' must be 1")
    input_directory = _absolute_directory(path, data, "input_directory")
    output_directory = _absolute_directory(path, data, "output_directory")
    if not input_directory.is_dir():
        raise ValueError(f"{path}: 'input_directory' does not exist or is not a directory: {input_directory}")
    if output_directory.exists() and not output_directory.is_dir():
        raise ValueError(f"{path}: 'output_directory' is not a directory: {output_directory}")
    write_job_records = data.get("write_job_records", False)
    if not isinstance(write_job_records, bool):
        raise ValueError(f"{path}: 'write_job_records' must be true or false")
    try:
        cooldown = parse_cooldown(data["cooldown"], "cooldown") if "cooldown" in data else None
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from error
    retention = data.get("history_retention_days", DEFAULT_HISTORY_RETENTION_DAYS)
    if not is_int(retention) or not 0 <= retention <= MAX_HISTORY_RETENTION_DAYS:
        raise ValueError(f"{path}: 'history_retention_days' must be a whole number of days from 0 to {MAX_HISTORY_RETENTION_DAYS}")
    api_limits = _api_limits(path, data.get("api_limits", {}))
    return GlobalConfig(input_directory=input_directory, output_directory=output_directory, write_job_records=write_job_records, cooldown=cooldown, history_retention_days=retention, api_limits=api_limits)


def _api_limits(path: Path, data: Any) -> ApiLimits:
    if not isinstance(data, dict):
        raise ValueError(f"{path}: 'api_limits' must be a mapping")
    unknown = sorted(set(data) - API_LIMITS_KEYS)
    if unknown:
        raise ValueError(f"{path}: unknown key 'api_limits.{unknown[0]}'")
    max_queued_jobs = _positive_int(path, data, "max_queued_jobs", DEFAULT_MAX_QUEUED_JOBS)
    max_job_runs = _positive_int(path, data, "max_job_runs", DEFAULT_MAX_JOB_RUNS)
    max_job_file_bytes = _positive_int(path, data, "max_job_file_bytes", DEFAULT_MAX_JOB_FILE_BYTES)
    seconds = data.get("max_job_seconds", DEFAULT_MAX_JOB_SECONDS)
    if not is_number(seconds) or seconds <= 0:
        raise ValueError(f"{path}: 'api_limits.max_job_seconds' must be a positive number")
    return ApiLimits(max_queued_jobs=max_queued_jobs, max_job_runs=max_job_runs, max_job_seconds=seconds, max_job_file_bytes=max_job_file_bytes)


def _positive_int(path: Path, data: dict[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if not is_int(value) or value <= 0:
        raise ValueError(f"{path}: 'api_limits.{key}' must be a positive whole number")
    return value


def _absolute_directory(path: Path, data: dict[str, Any], key: str) -> Path:
    value = data[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path}: '{key}' must be a non-empty path")
    directory = Path(value).expanduser()
    if not directory.is_absolute():
        raise ValueError(f"{path}: '{key}' must be an absolute path (a leading ~ is allowed): {value}")
    return directory
