"""Load the machine-specific global configuration for jobs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from draw_things_control.core.cooldown import CooldownPolicy, parse_cooldown, replaced_cooldown_message
from draw_things_control.core.numbers import is_int
from draw_things_control.core.paths import DEFAULT_PATHS
from draw_things_control.core.yaml_files import read_yaml_file

PROJECT_ROOT = DEFAULT_PATHS.root
DEFAULT_GLOBAL_CONFIG = DEFAULT_PATHS.global_config
EXAMPLE_GLOBAL_CONFIG = DEFAULT_PATHS.example_global_config
GLOBAL_CONFIG_KEYS = {"version", "input_directory", "output_directory", "write_job_records", "cooldown", "history_retention_days"}
DEFAULT_HISTORY_RETENTION_DAYS = 14
MAX_HISTORY_RETENTION_DAYS = 3650
REQUIRED_GLOBAL_CONFIG_KEYS = ("input_directory", "output_directory", "version")


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


def load_global_config(path: Path = DEFAULT_GLOBAL_CONFIG) -> GlobalConfig:
    """Parse and validate the global configuration file."""
    if not path.exists():
        hint = f"; copy {EXAMPLE_GLOBAL_CONFIG.relative_to(PROJECT_ROOT)} to {DEFAULT_GLOBAL_CONFIG.relative_to(PROJECT_ROOT)} and edit its paths" if path == DEFAULT_GLOBAL_CONFIG else ""
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
    return GlobalConfig(input_directory=input_directory, output_directory=output_directory, write_job_records=write_job_records, cooldown=cooldown, history_retention_days=retention)


def _absolute_directory(path: Path, data: dict[str, Any], key: str) -> Path:
    value = data[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path}: '{key}' must be a non-empty path")
    directory = Path(value).expanduser()
    if not directory.is_absolute():
        raise ValueError(f"{path}: '{key}' must be an absolute path (a leading ~ is allowed): {value}")
    return directory
