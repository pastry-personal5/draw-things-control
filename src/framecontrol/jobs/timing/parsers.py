"""What each backend's output lines say about its stages. A parser is made for each run, and reads one line at a time."""

from __future__ import annotations

import re
from typing import Protocol

from framecontrol.jobs.timing.model import Reading


class StageParser(Protocol):
    """Reads a backend's output lines as stage readings."""

    # Whether the backend starts each redraw with a carriage return and ends none, so a job log written before output was
    # stamped as read holds each reading one update after it was written (see ``log_import``).
    unterminated_redraws: bool

    def readings(self, text: str, offset: float) -> tuple[Reading, ...]:
        """The readings a line (without terminal codes or terminators) gives at ``offset``; none for a line that is not one."""
        ...

    def accepts(self, text: str) -> bool:
        """Whether a line that has not been terminated yet already reads as a whole reading, so the runner may release it
        once nothing more arrives (see ``DrawThingsProcessRunner``). False for a backend that terminates its lines."""
        ...


# Draw Things prints `Sampling... n / N [bar] p %` as step n starts, so n - 1 steps are done; `Finishing...` follows the last step.
_SAMPLING = re.compile(r"^Sampling\.\.\.\s+(\d+)\s*/\s*(\d+)(?!\d)")
_DRAW_THINGS_STAGES = (
    (re.compile(r"^Starting\.\.\."), "starting", "Starting", True),
    (re.compile(r"^Processing\.\.\."), "processing", "Processing", True),
    (re.compile(r"^Finishing\.\.\."), "finishing", "Finishing", True),
    # `Generated` ends the finishing the CLI began; it starts nothing.
    (re.compile(r"^Generated\b"), "finishing", "Generated", False),
)


class DrawThingsStageParser:
    """Draw Things: ``Starting...``, ``Processing...``, ``Sampling... n / N``, ``Finishing...``, then ``Generated``."""

    unterminated_redraws = False

    def readings(self, text: str, offset: float) -> tuple[Reading, ...]:
        sampling = _SAMPLING.match(text)
        if sampling is not None:
            step, steps = int(sampling.group(1)), int(sampling.group(2))
            if 1 <= step <= steps:
                return (Reading("sampling", "Sampling", offset, step - 1, steps, "steps", begins=True),)
            return ()
        for pattern, stage, phase, begins in _DRAW_THINGS_STAGES:
            if pattern.match(text):
                return (Reading(stage, phase, offset, begins=begins),)
        return ()

    def accepts(self, text: str) -> bool:
        return False


# Where H3's phases belong, as h3.c names them in its progress reports. A phase this table lacks is kept under its own name
# as `other`, so a newer H3 build never drops data silently.
H3_STAGES = {
    "tokenizer": "tokenize",
    "video vae encoder": "video_encode",
    "qwen vision": "vision",
    "text encoder": "text_encode",
    "refine text": "refine_text",
    "precompute adaln": "adaln",
    "load transformer core": "load_core",
    "denoise enqueue": "denoise",
    "denoise": "denoise",
    "audio vae encoder": "audio_encode",
    "audio vae": "audio_decode",
    "preview vae load": "preview_load",
    "video vae load": "video_load",
    "ffmpeg": "ffmpeg",
}
H3_UNITS = {"denoise": "steps", "ffmpeg": "frames"}
# The enqueue counter runs ahead of the step counter and is not the stage's progress.
H3_UNCOUNTED = frozenset({"denoise enqueue"})
# h3.c writes `"\r%-25s %4d/%-4d"` to stderr: the phase padded to 25 columns, then the number done and the total. Its other lines
# that hold an n/N (`h3: preview 3/30`, `h3: frame 5/24 via kitty`) start `h3:`, and a phase name never holds a colon.
_H3_LINE = re.compile(r"^(?P<phase>[A-Za-z][A-Za-z0-9 _.\-]*?)\s+(?P<done>\d+)\s*/\s*(?P<total>\d+)(?!\d)")


class H3StageParser:
    """H3: ``<phase> n/N``, where each phase counts its own units, and n of them are done (``0/N`` when the phase starts)."""

    unterminated_redraws = True

    def readings(self, text: str, offset: float) -> tuple[Reading, ...]:
        match = _H3_LINE.match(text)
        if match is None:
            return ()
        done, total = int(match.group("done")), int(match.group("total"))
        if total < 1 or done > total:
            return ()
        phase = match.group("phase").strip()
        key = phase.lower()
        stage = H3_STAGES.get(key, "other")
        return (Reading(stage, phase, offset, done, total, H3_UNITS.get(stage), counted=key not in H3_UNCOUNTED),)

    def accepts(self, text: str) -> bool:
        return bool(self.readings(text, 0.0))
