"""Numbers read from outside: a saved command, --config-json, a job manifest, or ffprobe's output."""

from __future__ import annotations

import math


def setting_number(value: object) -> float | None:
    """A finite number from JSON or from text (a flag's value, ffprobe's output); booleans are not numbers."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value) if isinstance(value, str | int | float) else None
    except (ValueError, OverflowError):
        # OverflowError: a JSON integer too large for a float.
        return None
    return number if number is not None and math.isfinite(number) else None


def positive_whole(value: object) -> int | None:
    """A positive whole number (a size, a frame or step count) as ``setting_number`` reads it; None for anything else."""
    number = setting_number(value)
    return int(number) if number is not None and number.is_integer() and number > 0 else None
