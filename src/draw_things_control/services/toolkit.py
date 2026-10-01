"""The real tools every front end uses: draw-things-cli, ffmpeg, and ffprobe, and the services built on them."""

from __future__ import annotations

import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from draw_things_control.core.arguments import DrawThingsGenerateArguments
from draw_things_control.core.draw_things_config import load_config
from draw_things_control.core.generation import GenerationService
from draw_things_control.core.process.output import MessageCallback, OutputProcessor
from draw_things_control.core.process.runner import ChildStartCallback, DrawThingsProcessRunner, RunnerFactory, StoppableRunner
from draw_things_control.jobs.executor import JobExecutor
from draw_things_control.jobs.media.checks import MediaChecker
from draw_things_control.jobs.media.frames import extract_last_frame
from draw_things_control.jobs.media.info import measure_output
from draw_things_control.jobs.media.stream_color import resolve_video_color
from draw_things_control.jobs.media.toolkit import MediaTools
from draw_things_control.jobs.media.tools import find_ffprobe, require_ffmpeg, require_ffprobe
from draw_things_control.jobs.media.video_color import tag_video_colors

if TYPE_CHECKING:
    from draw_things_control.jobs.color_run import CorrectionRequest, CorrectionResult, Corrector
    from draw_things_control.jobs.media.regions import Segmenter


def create_runner(arguments: DrawThingsGenerateArguments, timeout: float | None, shutdown_grace: float, on_message: MessageCallback | None = None, on_start: ChildStartCallback | None = None, *, handle_signals: bool = True) -> DrawThingsProcessRunner:
    """Connect the generation use case to its process adapter; ``on_message`` receives each line the child prints, ``on_start`` its PID and executable name."""
    # Without an output file, draw-things-cli previews in the terminal, so it must inherit it.
    capture_output = arguments.output is not None and not arguments.terminal_image
    name = Path(arguments.executable).name
    on_pid = (lambda pid: on_start(pid, name)) if on_start is not None else None
    return DrawThingsProcessRunner(arguments, output_processor=OutputProcessor(callback=on_message), timeout_seconds=timeout, shutdown_grace_seconds=shutdown_grace, capture_output=capture_output, handle_signals=handle_signals, on_start=on_pid)


def create_job_runner(arguments: DrawThingsGenerateArguments, timeout: float | None, shutdown_grace: float, on_message: MessageCallback | None = None, on_start: ChildStartCallback | None = None) -> DrawThingsProcessRunner:
    """Create a run's runner; JobExecutor owns signal handling and forwards signals to it."""
    return create_runner(arguments, timeout, shutdown_grace, on_message, on_start, handle_signals=False)


def default_media_tools() -> MediaTools:
    """The media tools of this machine: ffmpeg and ffprobe, found when each is used, the color correction, and Apple
    Vision's segmenter, which both the drift check and the correction use, made once when a check first needs it."""
    segmenter = _lazy_segmenter()
    checker = MediaChecker(lambda: shutil.which("ffmpeg"), segmenter=segmenter)
    return MediaTools(
        require_ffmpeg=require_ffmpeg,
        frame_extractor=extract_last_frame,
        color_reader=lambda video: resolve_video_color(video, shutil.which("ffmpeg"), find_ffprobe()),
        require_ffprobe=require_ffprobe,
        video_tagger=tag_video_colors,
        output_measurer=measure_output,
        checker=checker,
        corrector=_lazy_corrector(checker, segmenter),
    )


def _lazy_segmenter() -> Callable[[], Segmenter | None]:
    """Apple Vision's segmenter, made (loading pyobjc) the first time a check asks, once for the process; None where it
    is not available."""
    loaded: list[Segmenter | None] = []
    lock = threading.Lock()

    def segmenter() -> Segmenter | None:
        with lock:
            if not loaded:
                from draw_things_control.jobs.media.vision_segmenter import vision_segmenter

                loaded.append(vision_segmenter())
        return loaded[0]

    return segmenter


def _lazy_corrector(checker: MediaChecker, segmenter: Callable[[], Segmenter | None]) -> Corrector:
    """The color correction, loaded (with numpy) only when a job first corrects, so a front end starts without it."""
    loaded: list[Corrector] = []

    def correct(request: CorrectionRequest) -> CorrectionResult:
        if not loaded:
            from draw_things_control.jobs.color_run import ColorCorrector

            loaded.append(ColorCorrector(lambda: shutil.which("ffmpeg"), checker, segmenter=segmenter))
        return loaded[0](request)

    return correct


class Toolkit:
    """The tools of this machine. Built once by a front end, which asks it for the services that use them; a test builds one
    with fake tools, or overrides ``generation_service`` and ``job_executor``."""

    def __init__(self, *, find_executable: Callable[[str], str | None] = shutil.which, media: MediaTools | None = None, job_runner_factory: RunnerFactory[StoppableRunner] = create_job_runner) -> None:
        self._find_executable = find_executable
        self._media = media or default_media_tools()
        self._job_runner_factory = job_runner_factory

    def generation_service(self) -> GenerationService:
        """The service behind ``generate``: one draw-things-cli run, with the caller's terminal for its preview."""
        return GenerationService(runner_factory=create_runner, find_executable=self._find_executable, config_loader=load_config)

    def job_executor(self, *, handle_signals: bool = True) -> JobExecutor:
        """A job executor; ``handle_signals`` must be False for jobs run off the main thread."""
        return JobExecutor(runner_factory=self._job_runner_factory, find_executable=self._find_executable, media=self._media, handle_signals=handle_signals)
