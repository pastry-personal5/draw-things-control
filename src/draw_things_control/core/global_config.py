"""Load the machine-specific global configuration for jobs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_GLOBAL_CONFIG = PROJECT_ROOT / "config" / "global-config.yaml"
EXAMPLE_GLOBAL_CONFIG = PROJECT_ROOT / "config" / "global-config.example.yaml"
GLOBAL_CONFIG_KEYS = {"version", "input_directory", "output_directory", "write_job_records", "cooldown_seconds"}
REQUIRED_GLOBAL_CONFIG_KEYS = ("input_directory", "output_directory", "version")
MAX_COOLDOWN_SECONDS = 3600
COOLDOWN_ERROR = f"must be a number of seconds from 0 to {MAX_COOLDOWN_SECONDS}"


@dataclass(frozen=True)
class GlobalConfig:
    """Default directories for job inputs and outputs, and machine-wide job defaults."""

    input_directory: Path
    output_directory: Path
    write_job_records: bool = False
    # Seconds to wait between runs, unless a job sets its own cooldown_seconds.
    cooldown_seconds: float | None = None


def load_yaml_mapping(path: Path, description: str) -> dict[str, Any]:
    """Read a YAML file whose top level must be a mapping."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ValueError(f"{description} not found: {path}") from error
    except OSError as error:
        raise ValueError(f"Cannot read {description.lower()} {path}: {error.strerror}") from error
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ValueError(f"{description} is not valid YAML: {path} ({error})") from error
    if not isinstance(data, dict):
        raise ValueError(f"{description} must contain a YAML mapping: {path}")
    return data


def load_global_config(path: Path = DEFAULT_GLOBAL_CONFIG) -> GlobalConfig:
    """Parse and validate the global configuration file."""
    if not path.exists():
        hint = f"; copy {EXAMPLE_GLOBAL_CONFIG.relative_to(PROJECT_ROOT)} to {DEFAULT_GLOBAL_CONFIG.relative_to(PROJECT_ROOT)} and edit its paths" if path == DEFAULT_GLOBAL_CONFIG else ""
        raise ValueError(f"Global configuration not found: {path}{hint}")
    data = load_yaml_mapping(path, "Global configuration")
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
    cooldown_seconds = data.get("cooldown_seconds")
    if cooldown_seconds is not None and not is_cooldown(cooldown_seconds):
        raise ValueError(f"{path}: 'cooldown_seconds' {COOLDOWN_ERROR}")
    return GlobalConfig(input_directory=input_directory, output_directory=output_directory, write_job_records=write_job_records, cooldown_seconds=float(cooldown_seconds) if cooldown_seconds is not None else None)


def is_number(value: Any) -> bool:
    """Whether ``value`` is an int or float from YAML; booleans are not numbers here."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def is_cooldown(value: Any) -> bool:
    """Whether ``value`` is a valid cooldown: a number of seconds from 0 to MAX_COOLDOWN_SECONDS."""
    # The chained comparison also rejects NaN, and infinity is over the limit.
    return is_number(value) and 0 <= value <= MAX_COOLDOWN_SECONDS


def _absolute_directory(path: Path, data: dict[str, Any], key: str) -> Path:
    value = data[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path}: '{key}' must be a non-empty path")
    directory = Path(value).expanduser()
    if not directory.is_absolute():
        raise ValueError(f"{path}: '{key}' must be an absolute path (a leading ~ is allowed): {value}")
    return directory
