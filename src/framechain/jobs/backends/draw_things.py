"""Draw Things validation, command preparation, and media finishing for jobs."""

from __future__ import annotations

import json
import signal
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from framechain.core.arguments import DrawThingsGenerateArguments, override_arguments, redact_command
from framechain.core.draw_things_config import build_config_json, find_config_file
from framechain.core.generation import require_executable
from framechain.core.yaml_files import read_bounded_bytes
from framechain.jobs.backend import DRAW_THINGS_BACKEND, OutputPaths, RunContext, RunPreparation
from framechain.jobs.media.checks import MediaCheck
from framechain.jobs.media.toolkit import MediaTools
from framechain.jobs.output_naming import corrected_output_path, last_frame_path, raw_last_frame_path

if TYPE_CHECKING:
    from framechain.jobs.definition import ColorPolicy, ConfigOverride, JobDefinition, PromptPair
    from framechain.jobs.events import RunStatus
    from framechain.jobs.parsing import JobParser
    from framechain.jobs.planning import PlannedRun
    from framechain.jobs.records import RunRecord


@dataclass(frozen=True)
class DrawThingsJobSettings:
    """Validated Draw Things configuration, separate from the common chain fields."""

    config_file: str
    base_config: dict[str, Any]
    override: ConfigOverride
    model: str
    video_format: str | None
    color: ColorPolicy
    size: tuple[int, int] | None


class DrawThingsBackend:
    """All Draw Things command and output behavior needed by a chained job."""

    id = DRAW_THINGS_BACKEND
    supported_modes = frozenset({"i2i", "t2v", "i2v"})

    def parse_job(self, parser: JobParser, data: dict[str, Any], source_text: str) -> JobDefinition:
        return parser.parse_draw_things(data, source_text)

    def safe_settings(self, job: JobDefinition) -> dict[str, Any]:
        settings = cast(DrawThingsJobSettings, job.backend_data)
        summary: dict[str, Any] = {"model": settings.model, "output_extension": job.extension}
        if settings.video_format is not None:
            summary["video_format"] = settings.video_format
        if settings.size is not None:
            summary["width"], summary["height"] = settings.size
        return summary

    def snapshot_config(self, job: JobDefinition, params_directory: Path, max_bytes: int) -> str:
        config_path = find_config_file(job.config_file, params_directory)
        return read_bounded_bytes(config_path, max_bytes, "Configuration", key="config_file").decode("utf-8")

    def finishing_seconds(self, job: JobDefinition) -> float:
        return job.drift_seconds() + job.correction_seconds()

    def check_tools(self, job: JobDefinition, executable: str, media: MediaTools, find_executable: Callable[[str], str | None]) -> None:
        require_executable(find_executable, executable)
        if job.mode.is_video:
            media.require_ffmpeg()
            if media.require_ffprobe is not None:
                media.require_ffprobe()

    def output_paths(self, job: JobDefinition, output: Path) -> OutputPaths:
        if not job.mode.is_video:
            return OutputPaths()
        if job.color.corrects:
            return OutputPaths(last_frame_path(output), raw_last_frame_path(output), corrected_output_path(output))
        return OutputPaths(last_frame_path(output))

    def arguments(self, job: JobDefinition, pair: PromptPair, run_input: Path | None, output: Path, seed: int, executable: str, run_number: int = 1) -> DrawThingsGenerateArguments:
        settings = cast(DrawThingsJobSettings, job.backend_data)
        override = settings.override
        config = build_config_json(settings.base_config, override.as_dict())
        config["model"] = settings.model
        width, height = override.width, override.height
        if settings.size is not None:
            width, height = settings.size
            config["width"], config["height"] = settings.size
        flags = {**override_arguments(override.as_dict()), "model": settings.model, "width": width, "height": height, "seed": seed}
        return DrawThingsGenerateArguments(executable=executable, prompt=pair.positive, negative_prompt=pair.negative, config_json=json.dumps(config, separators=(",", ":")), image=run_input, output=output, video_format=settings.video_format, disable_preview=True, **flags)

    def prepare_run(self, job: JobDefinition, context: RunContext) -> RunPreparation:
        from framechain.jobs.run_finisher import RunColor

        notes = context.notes
        chain_anchor = context.chain_anchor
        if context.fresh:
            notes = ("A t2v job's run 1 has no input: it is measured within itself, not corrected, and its last frame becomes the first image.",)
        corrects = job.mode.is_video and job.color.corrects and not context.fresh and context.input is not None
        anchor = None
        reanchored = False
        if corrects and job.color.holds_to_anchor:
            if job.color.reanchor == "prompt_pair" and context.number > 1 and context.input is not None and context.previous_pair is not None and context.previous_pair.name != context.pair.name:
                chain_anchor = context.input
                reanchored = True
            elif chain_anchor is None:
                chain_anchor = context.first_image
            anchor = chain_anchor
        color = RunColor(first_image=context.first_image, anchor=anchor, reanchored=reanchored, notes=notes, corrects=corrects)
        return RunPreparation(finish_state=color, record_anchor=anchor, chain_anchor=chain_anchor)

    def redact(self, command: Sequence[str]) -> tuple[str, ...]:
        return tuple(redact_command(command))

    def finish(self, job: JobDefinition, run: PlannedRun, record: RunRecord, stop_requested: Callable[[], signal.Signals | None], finish_state: object | None, media: MediaTools, report: Callable[[int, MediaCheck], None] | None) -> tuple[RunStatus, int] | None:
        from framechain.jobs.run_finisher import RunColor, RunFinisher

        if not isinstance(finish_state, RunColor):
            raise TypeError("Draw Things finishing requires its prepared run state")
        return RunFinisher(media, report).finish(job, run, record, stop_requested, finish_state)
