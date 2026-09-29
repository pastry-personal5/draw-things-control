"""The terminal UI's own preferences (Milestone 04): today only the verbose level, kept in ``config/tui-preferences.yaml``."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, cast, get_args

import yaml

VerboseLevel = Literal["high", "medium", "low"]
VERBOSE_LEVELS: tuple[VerboseLevel, ...] = get_args(VerboseLevel)
DEFAULT_VERBOSE_LEVEL: VerboseLevel = "high"
# Two separate "1 minute"s: nothing ties them together beyond both being owner-chosen defaults.
MEDIUM_OUTPUT_WINDOW_SECONDS = 60
LOW_STATUS_REFRESH_SECONDS = 60
PREFERENCES_VERSION = 1


def wants_output(level: VerboseLevel) -> bool:
    """Whether ``WatchEvents`` should stream the child's own output lines (``run_output``) at this level."""
    return level != "low"


def load_verbose_level(path: Path) -> VerboseLevel:
    """The saved level; a missing file, unreadable YAML, or an unknown value is the default, never an error: a
    malformed preferences file is not worth blocking the TUI over."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return DEFAULT_VERBOSE_LEVEL
    level = data.get("verbose_level") if isinstance(data, dict) else None
    return cast(VerboseLevel, level) if level in VERBOSE_LEVELS else DEFAULT_VERBOSE_LEVEL


def save_verbose_level(path: Path, level: VerboseLevel) -> None:
    """Write the level, creating ``config/`` if it is missing. Raises ``OSError`` when it cannot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"version": PREFERENCES_VERSION, "verbose_level": level}), encoding="utf-8")
