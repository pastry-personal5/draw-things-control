"""Small, exact history rows for the fit and forecast tests: a run whose every duration the test chose."""

from __future__ import annotations

import math
from typing import Any

from framecontrol.jobs.timing.features import FRAMECONTROL, HistoryRun, HistoryStage

DT, H3 = "draw-things-cli", "h3"
BASE_PIXELS = 832 * 448


def dt_workload(*, pixels: int = BASE_PIXELS, frames: int | None = 81, steps: int = 10, guidance: float = 7.5, **extra: Any) -> dict[str, Any]:
    side = round(math.sqrt(pixels))
    return {"width": side, "height": pixels // side, "pixels": pixels, "frames": frames, "steps": steps, "mode": "i2v", "guidance_scale": guidance, **extra}


def h3_workload(*, pixels: int = BASE_PIXELS, frames: int = 243, steps: int = 30, references: int = 2, pipeline: str = "r2av", **extra: Any) -> dict[str, Any]:
    side = round(math.sqrt(pixels))
    return {"pipeline": pipeline, "width": side, "height": pixels // side, "pixels": pixels, "frames": frames, "steps": steps, "seconds": 10.0, "references": {"image": references} if references else {}, "reference_count": references, **extra}


def steps_stage(name: str, start: float, gaps: list[float], *, fidelity_marks: bool = True) -> HistoryStage:
    """An iterative stage that begins at ``start`` and takes ``gaps`` for its steps; marks at each step's start and at the end."""
    offsets = [start]
    for gap in gaps:
        offsets.append(round(offsets[-1] + gap, 6))
    marks = tuple((offset, index) for index, offset in enumerate(offsets)) if fidelity_marks else ()
    return HistoryStage(name, "child", start, offsets[-1], len(gaps), len(gaps), "steps", marks)


def dt_run(execution: int, run: int, *, step: float = 50.0, gaps: list[float] | None = None, workload: dict[str, Any] | None = None, environment: dict[str, Any] | None = None, fidelity: str = "live", finishing: float = 11.0, extra_stages: tuple[tuple[str, float], ...] = (("verify", 2.0),), output_frames: int | None = None) -> HistoryRun:
    """A Draw Things run: prepare 1 s, starting 7 s, processing 6 s, the steps, ``finishing``, then Framecontrol's own stages."""
    workload = workload if workload is not None else dt_workload()
    series = gaps if gaps is not None else [step] * workload["steps"]
    cursor = 1.0 + 7.0 + 6.0
    stages = [HistoryStage("prepare", FRAMECONTROL, 0.0, 1.0), HistoryStage("starting", "child", 1.0, 8.0), HistoryStage("processing", "child", 8.0, 14.0)]
    sampling = steps_stage("sampling", cursor, series)
    stages.append(sampling)
    cursor = sampling.end
    stages.append(HistoryStage("finishing", "child", cursor, cursor + finishing))
    cursor += finishing
    for name, seconds in extra_stages:
        stages.append(HistoryStage(name, FRAMECONTROL, round(cursor, 6), round(cursor + seconds, 6)))
        cursor += seconds
    return HistoryRun(execution, run, fidelity, DT, "i2v", workload, environment if environment is not None else {"model": "base.ckpt", "executable_bytes": 100, "executable_mtime": 1.0}, tuple(stages), output_frames)


def h3_run(execution: int, run: int, *, step: float = 60.0, gaps: list[float] | None = None, workload: dict[str, Any] | None = None, environment: dict[str, Any] | None = None, tail: float = 100.0, fidelity: str = "live") -> HistoryRun:
    """An H3 run: prepare 1 s, 20 s of setup phases, the denoise steps, ``tail`` of audio, video, and FFmpeg, then 2 s of Framecontrol."""
    workload = workload if workload is not None else h3_workload()
    series = gaps if gaps is not None else [step] * workload["steps"]
    stages = [HistoryStage("prepare", FRAMECONTROL, 0.0, 1.0)]
    cursor = 1.0
    for name, seconds, units in (("tokenize", 0.2, 1), ("text_encode", 4.8, 50), ("load_core", 15.0, 50)):
        stages.append(HistoryStage(name, "child", cursor, round(cursor + seconds, 6), units, units))
        cursor = round(cursor + seconds, 6)
    denoise = steps_stage("denoise", cursor, series)
    stages.append(denoise)
    cursor = denoise.end
    stages.append(HistoryStage("ffmpeg", "child", cursor, round(cursor + tail, 6), 243, 243, "frames"))
    cursor = round(cursor + tail, 6)
    stages.append(HistoryStage("verify", FRAMECONTROL, cursor, cursor + 2.0))
    return HistoryRun(execution, run, fidelity, H3, workload["pipeline"], workload, environment if environment is not None else {"model": "h3-model", "executable_bytes": 200, "executable_mtime": 2.0}, tuple(stages))


def log_run(execution: int, run: int, *, step: float = 50.0, pixels: int = BASE_PIXELS, frames: int = 81, steps: int = 10) -> HistoryRun:
    """A row rebuilt from a job log: the size, frames, and steps it measured, and no mode, no knobs, no model."""
    side = round(math.sqrt(pixels))
    workload = {"width": side, "height": pixels // side, "pixels": pixels, "frames": frames, "steps": steps}
    base = dt_run(execution, run, step=step, workload={**workload, "mode": "i2v", "guidance_scale": 7.5})
    return HistoryRun(execution, run, "log", DT, "i2v", workload, {}, base.stages)
