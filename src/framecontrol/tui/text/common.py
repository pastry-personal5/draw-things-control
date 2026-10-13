"""Styles and words the TUI's texts share."""

from __future__ import annotations

from framecontrol.jobs.definition import GenerationMode
from framecontrol.jobs.events import RunStatus

VIDEO_MODES = {mode.value for mode in GenerationMode if mode.is_video}


STATUS_STYLE = {RunStatus.RUNNING: "bold cyan", RunStatus.SUCCEEDED: "green", RunStatus.FAILED: "red", RunStatus.TIMED_OUT: "red", RunStatus.INTERRUPTED: "yellow", "pending": "dim", "parked": "blue", "parking": "yellow"}
