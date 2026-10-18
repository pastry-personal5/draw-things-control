"""What a front end shows of a published span, from the span and its own clock: the seconds left, the share done, and
whether the range is worth a mention. Every front end that shows a forecast reads it through here, so a bar and a countdown
mean the same thing in the TUI, in ``fc queue add --wait``, and in a client of the API."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from framecontrol.jobs.text import duration_text

# The range is shown when its half-width is over this share of the time left.
RANGE_SHARE = 0.03

Span = Mapping[str, float]


def seconds_left(span: Span, now: float) -> float:
    """The seconds to the span's end, never below zero."""
    return max(0.0, span["end_at"] - now)


def progress(span: Span, now: float) -> float:
    """The share done, 0 to 1: the share of the time from start to end that has passed, never below the floor the server
    held (which is all there is when the end is not after the start)."""
    length = span["end_at"] - span["started_at"]
    floor = span.get("floor", 0.0)
    if length <= 0:
        return floor
    return min(1.0, max(floor, (now - span["started_at"]) / length))


def show_range(span: Span, now: float) -> bool:
    """Whether the range is wide enough, against the time left, to be worth showing."""
    left = seconds_left(span, now)
    return left > 0 and (span["high"] - span["low"]) / 2 > RANGE_SHARE * left


def annotate(forecast: Mapping[str, Any], now: float) -> dict[str, Any]:
    """A published forecast with ``progress`` and ``seconds_left`` added to its run and job as of ``now``: what the HTTP read
    carries for a client that cannot work them out on a clock of its own (MCP imports nothing from the package)."""
    shown = dict(forecast)
    for name in ("run", "job"):
        span = forecast.get(name)
        if span is not None:
            shown[name] = {**span, "progress": progress(span, now), "seconds_left": seconds_left(span, now)}
    return shown


def whole_duration(seconds: float) -> str:
    """A duration in whole seconds, as a forecast writes it: ``7 min 12 s``, never tenths."""
    return duration_text(max(0, round(seconds)))


# A forecast whose range runs past this is shown as ending this far off: a date beyond the calendar cannot be written.
MAX_DISPLAY_SECONDS = 366 * 86400.0


def _bounded(seconds: float) -> float:
    """``seconds`` kept within a year either way, so adding it to a clock reading always gives a date."""
    return max(-MAX_DISPLAY_SECONDS, min(MAX_DISPLAY_SECONDS, seconds)) if seconds == seconds else 0.0


def _clock_time(moment: datetime, now: datetime) -> str:
    return moment.strftime("%H:%M") if moment.date() == now.date() else moment.strftime("%m-%d %H:%M")


def end_text(remaining: float, now: datetime, bounds: tuple[float, float] | None = None) -> str:
    """When something left ``remaining`` seconds from ``now`` ends: ``ends ~16:42 (in 23 min)``, dated when it is another day.
    With ``bounds`` (the least and most seconds it may have left) the range stands in for the time left:
    ``ends ~16:42 (16:31 to 16:58)``."""
    end = now + timedelta(seconds=_bounded(remaining))
    if bounds is not None:
        return f"ends ~{_clock_time(end, now)} ({_clock_time(now + timedelta(seconds=_bounded(bounds[0])), now)} to {_clock_time(now + timedelta(seconds=_bounded(bounds[1])), now)})"
    return f"ends ~{_clock_time(end, now)} (in {whole_duration(remaining)})"


def stage_text(forecast: Mapping[str, Any] | None) -> str | None:
    """The stage a forecast says the run is in, as its name with spaces and its units: ``denoise 12/30``; None when the job is
    between runs or has no forecast."""
    if forecast is None or forecast.get("stage") is None:
        return None
    name = str(forecast["stage"]).replace("_", " ")
    if forecast.get("stage_total") is None:
        return name
    return f"{name} {forecast.get('stage_done') or 0}/{forecast['stage_total']}"


def _end_phrase(span: Span, now: float) -> str:
    """``ends ~16:42 (in 23 min)`` for a span read at ``now``, or ``finishing`` once its end has passed. A span the server read
    for a client (it carries ``seconds_left``) is read at the moment the server did, so a clock that is off changes nothing."""
    left = span["seconds_left"] if "seconds_left" in span else seconds_left(span, now)
    if left <= 0:
        return "finishing"
    read_at = span["end_at"] - left
    bounds = (max(0.0, span["low"] - read_at), max(0.0, span["high"] - read_at)) if show_range(span, read_at) else None
    return end_text(left, datetime.fromtimestamp(read_at), bounds)


KNOBS_NOTES = {"provisional": "from provisional calibration", "pinned": "with pinned timing knobs"}


def knobs_note(forecast: Mapping[str, Any] | None) -> str | None:
    """Where a forecast's non-history numbers came from, when that is not the factory: ``from provisional calibration`` or
    ``with pinned timing knobs``; None for the factory and for a snapshot that predates the field."""
    basis = forecast.get("basis") if forecast is not None else None
    return KNOBS_NOTES.get(str(basis.get("knobs"))) if isinstance(basis, Mapping) else None


def forecast_text(forecast: Mapping[str, Any], now: float) -> str:
    """A forecast in one line: the stage and its units, and where the run and the job end (``denoise 12/30; run ends ~16:42
    (in 23 min), job ends ~18:10 (in 1 h 51 min)``), or that there is no estimate yet."""
    ends = [f"{name} {_end_phrase(forecast[name], now)}" for name in ("run", "job") if forecast.get(name) is not None]
    stage = stage_text(forecast)
    if not ends:
        ends = ["no estimate yet"]
    return "; ".join(part for part in (stage, ", ".join(ends), knobs_note(forecast)) if part)
