"""What happens to a run's output after draw-things-cli succeeded: color tags, the last frame, the measurement, and,
the checks of the video and its last frame."""

from __future__ import annotations

import signal
import struct
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from draw_things_control.core.exit_codes import exit_code_for_signal
from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.jobs.events import RunStatus
from draw_things_control.jobs.media.checks import MediaCheck
from draw_things_control.jobs.media.stream_color import StreamColor
from draw_things_control.jobs.media.toolkit import MediaTools
from draw_things_control.jobs.planning import PlannedRun
from draw_things_control.jobs.records import RunRecord


class RunFinisher:
    """Finishes a successful run: tags a video's colors, extracts its last frame, and measures what the file holds."""

    def __init__(self, media: MediaTools, report: Callable[[int, MediaCheck], None] | None = None) -> None:
        self._media = media
        # Receives each media check with its run's number; the executor makes it an event.
        self._report = report

    def finish(self, job: JobDefinition, run: PlannedRun, record: RunRecord, stop_requested: Callable[[], signal.Signals | None]) -> tuple[RunStatus, int] | None:
        """Finish ``run``, updating ``record``; None when it succeeded, else the run's status and exit code.

        A failed frame extraction fails the run, unless a stop was requested meanwhile: then the run was interrupted.
        """
        video = job.mode.is_video
        checker = self._media.checker if video and self._report is not None else None
        # Read before the tagger, which adds a colr box that ffprobe would then report instead of the stream's own.
        color = self._media.color_reader(run.output) if video and self._media.color_reader is not None else StreamColor()
        written = checker.probe(run.output) if checker is not None else None
        if self._media.video_tagger is not None and video:
            self._tag_video(run.output, color)
        tagged = None
        if checker is not None and written is not None:
            check, tagged = checker.output(run.output, job.video_format, written, color)
            self._reported(run.number, check)
        if run.last_frame is not None:
            try:
                self._media.frame_extractor(run.output, run.last_frame, color)
            except ValueError as error:
                stop = stop_requested()
                if stop is not None:
                    return RunStatus.INTERRUPTED, exit_code_for_signal(stop)
                logger.error("{}", error)
                return RunStatus.FAILED, 1
            record.last_frame = run.last_frame.name
            if checker is not None:
                self._reported(run.number, checker.last_frame(run.last_frame, tagged, color))
        self._measure(run.output, record)
        return None

    def _reported(self, number: int, check: MediaCheck) -> None:
        if self._report is not None:
            self._report(number, check)

    def _measure(self, output: Path, record: RunRecord) -> None:
        """Record the output's actual size and frame count; a file that cannot be measured is a warning, never a failed run."""
        if self._media.output_measurer is None:
            return
        try:
            info = self._media.output_measurer(output)
        except (ValueError, OSError) as error:
            logger.warning("Could not measure {}; its size and frame count are not recorded: {}", output.name, error)
            return
        record.output_width, record.output_height, record.output_frames = info.width, info.height, info.frames

    def _tag_video(self, video: Path, color: StreamColor) -> None:
        """Label the video's colors as its stream states them; a video that cannot be tagged is kept as Draw Things wrote
        it, and the run still succeeds."""
        assert self._media.video_tagger is not None
        try:
            self._media.video_tagger(video, color)
        except (ValueError, OSError, struct.error) as error:
            logger.warning("Could not write color tags into {}; it keeps the tags Draw Things wrote: {}", video.name, error)
