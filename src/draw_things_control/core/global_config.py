"""Load the machine-specific global configuration for jobs."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_GLOBAL_CONFIG = PROJECT_ROOT / "config" / "global-config.yaml"
EXAMPLE_GLOBAL_CONFIG = PROJECT_ROOT / "config" / "global-config.example.yaml"
GLOBAL_CONFIG_KEYS = {"version", "input_directory", "output_directory", "write_job_records", "cooldown", "history_retention_days"}
DEFAULT_HISTORY_RETENTION_DAYS = 14
MAX_HISTORY_RETENTION_DAYS = 3650
REQUIRED_GLOBAL_CONFIG_KEYS = ("input_directory", "output_directory", "version")
MAX_COOLDOWN_SECONDS = 3600
COOLDOWN_ERROR = f"must be a number of seconds from 0 to {MAX_COOLDOWN_SECONDS}"
DEFAULT_COOLDOWN_RATIO = 0.5
# The keys each cooldown mode takes, besides mode itself.
COOLDOWN_MODE_KEYS = {"auto": ("ratio", "minimum_seconds", "maximum_seconds"), "manual": ("seconds",), "off": ()}
COOLDOWN_KEYS = {"mode", *(key for keys in COOLDOWN_MODE_KEYS.values() for key in keys)}


@dataclass(frozen=True)
class CooldownWait:
    """The wait after one run: its seconds, and which bound set them (``minimum``, ``maximum``, or None)."""

    seconds: float
    bound: str | None = None


@dataclass(frozen=True)
class CooldownPolicy:
    """How long a job waits after each successful run but the last.

    ``auto`` waits ``ratio`` of the run's time, rounded up to a whole second and kept between the bounds;
    ``manual`` waits a fixed ``seconds``; ``off`` never waits.
    """

    mode: str
    ratio: float = DEFAULT_COOLDOWN_RATIO
    seconds: float = 0.0
    minimum_seconds: float = 0.0
    maximum_seconds: float = float(MAX_COOLDOWN_SECONDS)

    def wait_after(self, run_seconds: float) -> CooldownWait:
        """The wait after a run that took ``run_seconds``."""
        if self.mode == "manual":
            return CooldownWait(self.seconds)
        if self.mode == "off":
            return CooldownWait(0.0)
        # Decimal, so a share that is a whole number in decimal is not rounded up by a binary remainder (1200 x 0.1 is 120, not 121).
        share = math.ceil(Decimal(repr(float(run_seconds))) * Decimal(repr(float(self.ratio))))
        if share < self.minimum_seconds:
            return CooldownWait(self.minimum_seconds, "minimum")
        if share > self.maximum_seconds:
            return CooldownWait(self.maximum_seconds, "maximum")
        return CooldownWait(float(share))

    @property
    def fixed_seconds(self) -> float | None:
        """The wait as the old single number: manual's seconds, 0 for off, None for auto."""
        return {"manual": self.seconds, "off": 0.0}.get(self.mode)

    def as_dict(self) -> dict[str, Any]:
        """The mapping as resolved, defaults filled in, as a job or global configuration would write it."""
        return {"mode": self.mode, **{key: getattr(self, key) for key in COOLDOWN_MODE_KEYS[self.mode]}}


DEFAULT_COOLDOWN = CooldownPolicy(mode="auto")


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


def load_yaml_mapping(path: Path, description: str) -> dict[str, Any]:
    """Read a YAML file whose top level must be a mapping."""
    return read_yaml_mapping(path, description)[0]


def read_yaml_mapping(path: Path, description: str) -> tuple[dict[str, Any], str]:
    """Read a YAML file whose top level must be a mapping; return the mapping and the file's text."""
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
    return data, text


def load_global_config(path: Path = DEFAULT_GLOBAL_CONFIG) -> GlobalConfig:
    """Parse and validate the global configuration file."""
    if not path.exists():
        hint = f"; copy {EXAMPLE_GLOBAL_CONFIG.relative_to(PROJECT_ROOT)} to {DEFAULT_GLOBAL_CONFIG.relative_to(PROJECT_ROOT)} and edit its paths" if path == DEFAULT_GLOBAL_CONFIG else ""
        raise ValueError(f"Global configuration not found: {path}{hint}")
    data = load_yaml_mapping(path, "Global configuration")
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
    if not isinstance(retention, int) or isinstance(retention, bool) or not 0 <= retention <= MAX_HISTORY_RETENTION_DAYS:
        raise ValueError(f"{path}: 'history_retention_days' must be a whole number of days from 0 to {MAX_HISTORY_RETENTION_DAYS}")
    return GlobalConfig(input_directory=input_directory, output_directory=output_directory, write_job_records=write_job_records, cooldown=cooldown, history_retention_days=retention)


def is_number(value: Any) -> bool:
    """Whether ``value`` is an int or float from YAML; booleans are not numbers here."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def is_cooldown(value: Any) -> bool:
    """Whether ``value`` is a valid cooldown: a number of seconds from 0 to MAX_COOLDOWN_SECONDS."""
    # The chained comparison also rejects NaN, and infinity is over the limit.
    return is_number(value) and 0 <= value <= MAX_COOLDOWN_SECONDS


def parse_cooldown(value: Any, key: str) -> CooldownPolicy:
    """Validate a ``cooldown`` mapping from a job or the global configuration; errors name ``key`` and the bad entry."""
    if not isinstance(value, dict):
        raise ValueError(f"'{key}' must be a mapping with a mode: auto, manual, or off")
    if "mode" not in value:
        raise ValueError(f"'{key}.mode' is required: auto, manual, or off")
    mode = value["mode"]
    # YAML reads an unquoted off as false.
    if mode is False:
        mode = "off"
    if not isinstance(mode, str) or mode not in COOLDOWN_MODE_KEYS:
        raise ValueError(f"'{key}.mode' must be auto, manual, or off")
    allowed = COOLDOWN_MODE_KEYS[mode]
    for name in value:
        if name not in COOLDOWN_KEYS:
            raise ValueError(f"'{key}.{name}' is not a known key")
        if name != "mode" and name not in allowed:
            raise ValueError(f"'{key}.{name}' is not used with mode {mode}")
    if mode == "manual" and "seconds" not in value:
        raise ValueError(f"'{key}.seconds' is required with mode manual")
    for name in ("seconds", "minimum_seconds", "maximum_seconds"):
        if name in value and not is_cooldown(value[name]):
            raise ValueError(f"'{key}.{name}' {COOLDOWN_ERROR}")
    ratio = value.get("ratio", DEFAULT_COOLDOWN_RATIO)
    if not is_number(ratio) or not 0 < ratio <= 1:
        raise ValueError(f"'{key}.ratio' must be a number above 0 and up to 1")
    policy = CooldownPolicy(
        mode=mode,
        ratio=float(ratio),
        seconds=float(value.get("seconds", 0.0)),
        minimum_seconds=float(value.get("minimum_seconds", 0.0)),
        maximum_seconds=float(value.get("maximum_seconds", MAX_COOLDOWN_SECONDS)),
    )
    if policy.minimum_seconds > policy.maximum_seconds:
        raise ValueError(f"'{key}.minimum_seconds' must not be above '{key}.maximum_seconds' ({_number_text(policy.maximum_seconds)})")
    return policy


def replaced_cooldown_message(value: Any) -> str:
    """The error for the old ``cooldown_seconds`` key, with the mapping that keeps the same wait."""
    if is_cooldown(value) and value == 0:
        same = "{mode: off}"
    else:
        same = f"{{mode: manual, seconds: {_number_text(value) if is_cooldown(value) else 900}}}"
    return f"'cooldown_seconds' was replaced by 'cooldown'; write cooldown: {same} for the same wait, or use mode auto or off (see {EXAMPLE_GLOBAL_CONFIG.relative_to(PROJECT_ROOT)})"


def _number_text(value: float) -> str:
    """A number as a person writes it in YAML: ``1200``, ``90.5``."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _absolute_directory(path: Path, data: dict[str, Any], key: str) -> Path:
    value = data[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path}: '{key}' must be a non-empty path")
    directory = Path(value).expanduser()
    if not directory.is_absolute():
        raise ValueError(f"{path}: '{key}' must be an absolute path (a leading ~ is allowed): {value}")
    return directory
