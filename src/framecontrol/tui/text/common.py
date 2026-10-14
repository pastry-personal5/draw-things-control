"""Styles and words the TUI's texts share."""

from __future__ import annotations

from framecontrol.jobs.definition import GenerationMode
from framecontrol.jobs.events import RunStatus

# A version-2 job's mode is its one pipeline (r2av is video too) or ``mixed``, which has video runs unless it is all stills.
VIDEO_MODES = {mode.value for mode in GenerationMode if mode.is_video} | {"r2av", "mixed"}


STATUS_STYLE = {RunStatus.RUNNING: "bold cyan", RunStatus.SUCCEEDED: "green", RunStatus.FAILED: "red", RunStatus.TIMED_OUT: "red", RunStatus.INTERRUPTED: "yellow", "pending": "dim", "parked": "blue", "parking": "yellow"}
