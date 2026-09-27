"""The errors every front end reports, each with a stable code that a front end maps to its own status."""

from __future__ import annotations

from pathlib import Path


class DtcError(Exception):
    """An error a front end shows to its user instead of crashing. ``code`` says what kind it is."""

    code = "error"


class InputError(DtcError, ValueError):
    """Invalid input: a job file, a configuration, an option, or an input file. Still a ValueError, so code that catches one still does.

    ``field`` names the offending key (``config_override.steps``) and ``path`` the file, when the error has them; the text
    already says both.
    """

    code = "invalid_input"

    def __init__(self, message: str, *, field: str | None = None, path: Path | None = None) -> None:
        super().__init__(message)
        self.field = field
        self.path = path


class ToolMissingError(InputError):
    """``draw-things-cli``, ``ffmpeg``, or ``ffprobe`` cannot be found."""

    code = "tool_missing"


class NotFoundError(InputError):
    """No such job, job ID, or execution ID."""

    code = "not_found"


class BusyError(DtcError):
    """Another run holds the run lock, or an earlier run's child is still running."""

    code = "busy"


class StateUnavailableError(DtcError):
    """The state database or the lock file cannot be used."""

    code = "state_unavailable"
