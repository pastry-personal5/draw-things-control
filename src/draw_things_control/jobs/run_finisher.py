"""What happens to a run's output after draw-things-cli succeeded: color tags, the last frame, the measurement, the
checks of the video and its last frame, and its color drift."""

from __future__ import annotations

import signal
import struct
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from draw_things_control.core.exit_codes import exit_code_for_signal
from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.jobs.events import RunStatus
from draw_things_control.jobs.media.checks import MediaCheck
from draw_things_control.jobs.media.stream_color import StreamColor
from draw_things_control.jobs.media.toolkit import MediaTools
from draw_things_control.jobs.planning import PlannedRun
from draw_things_control.jobs.records import RunRecord

if TYPE_CHECKING:
    from draw_things_control.jobs.color_run import CorrectionResult


@dataclass(frozen=True)
class RunColor:
    """What a run's colors are compared with and held to (Milestone 09), from the executor, which tracks the chain."""

    # The chain's first image; None when there is none yet (a t2v job's run 1) or none at all.
    first_image: Path | None = None
    # The file the run is corrected toward; None when the job does not correct toward an anchor.
    anchor: Path | None = None
    # Whether this run made its own input the anchor.
    reanchored: bool = False
    # Notes for the color_drift check, such as why a comparison is left out.
    notes: tuple[str, ...] = ()
    # Whether this run is corrected: its job asks, and it has an input (a t2v job's run 1 has none).
    corrects: bool = False


class RunFinisher:
    """Finishes a successful run: tags a video's colors, extracts its last frame, and measures what the file holds."""

    def __init__(self, media: MediaTools, report: Callable[[int, MediaCheck], None] | None = None) -> None:
        self._media = media
        # Receives each media check with its run's number; the executor makes it an event.
        self._report = report

    def finish(self, job: JobDefinition, run: PlannedRun, record: RunRecord, stop_requested: Callable[[], signal.Signals | None], run_color: RunColor | None = None) -> tuple[RunStatus, int] | None:
        """Finish ``run``, updating ``record``; None when it succeeded, else the run's status and exit code.

        A failed frame extraction fails the run, unless a stop was requested meanwhile: then the run was interrupted.
        """
        run_color = run_color or RunColor()
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
            corrector = self._media.corrector
            correcting = run_color.corrects and corrector is not None and run.raw_last_frame is not None and run.corrected_output is not None and run.input is not None
            # A correcting run extracts the uncorrected frame beside the handoff, which the correction then writes.
            extracted = run.raw_last_frame if correcting and run.raw_last_frame is not None else run.last_frame
            try:
                self._media.frame_extractor(run.output, extracted, color)
            except ValueError as error:
                stop = stop_requested()
                if stop is not None:
                    return RunStatus.INTERRUPTED, exit_code_for_signal(stop)
                logger.error("{}", error)
                return RunStatus.FAILED, 1
            result = self._correct(job, run, record, run_color, color) if correcting else None
            if result is None and extracted != run.last_frame:
                return RunStatus.FAILED, 1
            record.last_frame = run.last_frame.name
            if checker is not None:
                self._reported(run.number, checker.last_frame(run.last_frame, tagged, color))
            # Every video run measures its color drift, whether or not its job corrects (owner decision); a correction
            # measures it in its own first pass.
            drift = result.drift if result is not None else None
            if drift is not None:
                self._reported(run.number, drift)
            elif checker is not None:
                self._reported(run.number, checker.color_drift(run.output, color, run.input, run_color.first_image, run_color.notes))
            for check in result.checks if result is not None else ():
                self._reported(run.number, check)
        self._measure(run.output, record)
        return None

    def _correct(self, job: JobDefinition, run: PlannedRun, record: RunRecord, run_color: RunColor, color: StreamColor) -> CorrectionResult | None:
        """Correct the run's colors (Milestone 09), writing the handoff and the copy; a failed correction hands off the
        uncorrected frame instead, and the run still succeeds (owner decision). A stop or park requested meanwhile
        takes effect when the correction ends, as one during the extraction does. None only when the uncorrected frame
        could not be moved into the handoff's place."""
        # Imported here, as the resize is, so a job that never corrects does not load numpy for it.
        from draw_things_control.jobs.color_run import CorrectionRequest

        assert self._media.corrector is not None and run.last_frame is not None and run.raw_last_frame is not None and run.corrected_output is not None and run.input is not None
        request = CorrectionRequest(video=run.output, color=color, run_input=run.input, handoff=run.last_frame, copy=run.corrected_output, policy=job.color, anchor=run_color.anchor, reanchored=run_color.reanchored, first_image=run_color.first_image, drift_notes=run_color.notes)
        result = self._media.corrector(request)
        if result.succeeded:
            record.corrected_output = run.corrected_output.name
            return result
        try:
            run.raw_last_frame.rename(run.last_frame)
        except OSError as error:
            logger.error("Could not hand off the uncorrected last frame {}: {}", run.raw_last_frame.name, error)
            return None
        logger.warning("The color correction of {} failed; the uncorrected last frame is the handoff", run.output.name)
        return result

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
