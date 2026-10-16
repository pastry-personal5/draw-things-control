"""A seeded generator of run histories for both backends, written from the spreads Milestone 10 recorded.

The numbers come from ``docs/research/time-estimation.md``: H3 setup of 17 to 27 s, 30 steps of 19 to 75 s whose time grows
faster than the clip length (about 21 s at 5 s, 45 s at 8 s, 65 s at 10 s), 66 to 142 s of audio, video, and FFmpeg after the
last step, 1 to 2 s of Framecontrol finishing (about 42 s with color correction); Draw Things at about 50 s per step whose time
rose 11% to 24% in seven runs of eight, a first sampling reading 9 to 17 s in, and 11 s of decode.

The truth is log-linear in the workload with a per-run speed factor that has an execution part and a run part, as the
estimator assumes. It also holds what the estimator does not assume, so coverage is not tested against its own model: a
heavy-tailed run factor, drift between executions, one Draw Things run in eight with a falling step time, H3's short first
gap, an occasional long pause, signatures that appear once or twice, and a rebuilt executable that shifts H3 from some
execution on. The parameters were fixed from the evidence before the estimator was measured on them."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from framecontrol.core.cooldown import CooldownPolicy
from framecontrol.jobs.timing.features import FRAMECONTROL, HistoryExecution, HistoryRun, HistoryStage

DRAW_THINGS, H3 = "draw-things-cli", "h3"
P0, DT_F0, H3_F0 = 832 * 448, 81, 243
CHILD = "child"
COOLDOWNS = (CooldownPolicy("auto", ratio=0.5, minimum_seconds=60.0, maximum_seconds=900.0), CooldownPolicy("auto", ratio=0.25), CooldownPolicy("manual", seconds=120.0), CooldownPolicy("off"))

# Run speed factor, in the log: an execution part that drifts, and a heavy-tailed part of the run's own.
EXECUTION_DRIFT = (0.9, 0.02)
EXECUTION_NOISE = 0.025
RUN_SCALE = 0.035
STEP_NOISE = 0.04
PAUSE_RATE = 0.05
REBUILD_AT = 0.62
REBUILD_SHIFT = 0.06
# A signature of less weight than this is rare: it is placed in one execution of one or two runs, not drawn.
RARE_BELOW = 0.1


@dataclass(frozen=True)
class Kind:
    """One signature of the history: what it runs, and how likely a job is to use it."""

    backend: str
    pipeline: str
    workload: dict[str, Any]
    model: str
    weight: float
    correct: bool = False

    @property
    def pixels(self) -> int:
        return self.workload["pixels"]

    @property
    def frames(self) -> int:
        return self.workload["frames"]


@dataclass(frozen=True)
class SyntheticHistory:
    executions: tuple[HistoryExecution, ...]
    kinds: tuple[Kind, ...] = field(default=())

    @property
    def runs(self) -> list[HistoryRun]:
        return [run for execution in self.executions for run in execution.runs]


def _draw_things_kind(width: int, height: int, frames: int, steps: int, guidance: float, weight: float, correct: bool = False) -> Kind:
    workload = {"width": width, "height": height, "pixels": width * height, "frames": frames, "steps": steps, "mode": "i2v", "guidance_scale": guidance}
    return Kind(DRAW_THINGS, "i2v", workload, "base.ckpt", weight, correct)


def _h3_kind(pipeline: str, width: int, height: int, seconds: float, frames: int, layers: int, core_reuse: bool, weight: float, correct: bool = False, references: int = 0) -> Kind:
    workload = {
        "pipeline": pipeline,
        "width": width,
        "height": height,
        "pixels": width * height,
        "frames": frames,
        "steps": 30,
        "seconds": seconds,
        "references": {"image": references} if references else {},
        "reference_count": references,
        "render_width": width // 2,
        "render_height": height // 2,
        "denoise_reuse": 1,
        "dit_layers": layers,
        "core_reuse": core_reuse,
        "token_reduction": 0.0,
        "ssd_streaming": False,
        "int8_row_fc2": True,
        "reference_rope": True,
        "reference_image_size": 448,
        "knobs": {"frames": frames, "seconds": seconds},
    }
    return Kind(H3, pipeline, workload, "h3-model", weight, correct)


def catalog() -> tuple[Kind, ...]:
    """The signatures: some used by many jobs, some by one or two."""
    return (
        _draw_things_kind(832, 448, 81, 40, 7.5, 10.0),
        _draw_things_kind(832, 448, 81, 40, 5.0, 4.0),
        _draw_things_kind(832, 448, 41, 40, 7.5, 5.0, correct=True),
        _draw_things_kind(832, 448, 121, 30, 7.5, 3.0),
        _draw_things_kind(640, 384, 81, 40, 7.5, 6.0),
        _draw_things_kind(640, 384, 41, 30, 5.0, 2.0),
        _draw_things_kind(1024, 576, 81, 40, 7.5, 2.5, correct=True),
        _draw_things_kind(1024, 576, 41, 40, 7.5, 1.5),
        _draw_things_kind(512, 320, 81, 30, 7.5, 1.0),
        _draw_things_kind(832, 448, 81, 30, 3.5, 0.02),
        _draw_things_kind(640, 384, 121, 40, 7.5, 0.02),
        _draw_things_kind(1024, 576, 121, 30, 5.0, 0.02),
        _h3_kind("r2av", 832, 448, 10.0, 243, 36, True, 10.0, references=2),
        _h3_kind("r2av", 832, 448, 10.0, 243, 36, True, 3.0, correct=True, references=3),
        _h3_kind("r2av", 832, 448, 8.0, 193, 36, True, 5.0, references=2),
        _h3_kind("r2av", 832, 448, 5.0, 121, 36, True, 4.0, references=1),
        _h3_kind("r2av", 640, 384, 8.0, 193, 36, True, 3.0, references=2),
        _h3_kind("r2av", 640, 384, 10.0, 243, 28, True, 2.0, references=2),
        _h3_kind("i2av", 832, 448, 10.0, 243, 36, True, 8.0),
        _h3_kind("i2av", 832, 448, 8.0, 193, 36, True, 6.0, correct=True),
        _h3_kind("i2av", 832, 448, 5.0, 121, 36, True, 4.0),
        _h3_kind("i2av", 640, 384, 8.0, 193, 36, False, 2.0),
        _h3_kind("i2av", 640, 384, 5.0, 121, 28, True, 0.02),
        _h3_kind("r2av", 832, 448, 5.0, 121, 28, False, 0.02, references=3),
        _h3_kind("i2av", 832, 448, 10.0, 243, 28, True, 0.02),
    )


def _log_median_step(kind: Kind) -> float:
    """The truth for a signature: the log of a typical step's seconds."""
    pixels = math.log(kind.pixels / P0)
    if kind.backend == DRAW_THINGS:
        return math.log(50.0) + 1.15 * pixels + 1.10 * math.log(kind.frames / DT_F0) + 0.30 * math.log(kind.workload["guidance_scale"] / 7.5)
    workload = kind.workload
    return math.log(69.0) - 0.13 + 0.9 * pixels + 1.65 * math.log(kind.frames / H3_F0) + 0.12 * math.log1p(workload["reference_count"]) - 0.15 * (kind.pipeline == "i2av") + math.log(workload["dit_layers"] / 36) - 0.10 * workload["core_reuse"]


class _Generator:
    def __init__(self, seed: int, executions: int) -> None:
        self.rng = np.random.default_rng(seed)
        self.executions = executions
        self.kinds = catalog()
        self.drift = {DRAW_THINGS: 0.0, H3: 0.0}
        rare = [kind for kind in self.kinds if kind.weight < RARE_BELOW]
        slots = self.rng.choice(np.arange(1, executions + 1), size=len(rare), replace=False)
        self.rare = {int(number): kind for number, kind in zip(slots, rare, strict=True)}

    def normal(self, scale: float = 1.0) -> float:
        return float(self.rng.normal(0.0, scale))

    def lognormal(self, median: float, spread: float) -> float:
        return round(median * math.exp(self.normal(spread)), 6)

    def build(self) -> SyntheticHistory:
        result = []
        for number in range(1, self.executions + 1):
            result.append(self.execution(number))
        return SyntheticHistory(tuple(result), self.kinds)

    def pick(self, backend: str) -> Kind:
        pool = [kind for kind in self.kinds if kind.backend == backend and kind.weight >= RARE_BELOW]
        weights = np.array([kind.weight for kind in pool])
        return pool[int(self.rng.choice(len(pool), p=weights / weights.sum()))]

    def execution(self, number: int) -> HistoryExecution:
        rare = self.rare.get(number)
        backend = rare.backend if rare is not None else DRAW_THINGS if self.rng.random() < 0.4 else H3
        first = rare if rare is not None else self.pick(backend)
        count = int(self.rng.integers(1, 3 if rare is not None else 7))
        kinds = [first] * count
        if backend == H3 and count > 1 and self.rng.random() < 0.3:
            # A mixed chain: the other pipeline of the same size and clip, where there is one.
            other = next((kind for kind in self.kinds if kind.backend == H3 and kind.pipeline != first.pipeline and kind.pixels == first.pixels and kind.frames == first.frames), None)
            if other is not None:
                kinds = [first if index % 2 == 0 else other for index in range(count)]
        # The machine's state over this execution: it drifts from one to the next.
        phi, sigma = EXECUTION_DRIFT
        self.drift[backend] = phi * self.drift[backend] + self.normal(sigma)
        shared = self.drift[backend] + self.normal(EXECUTION_NOISE)
        rebuilt = backend == H3 and number > REBUILD_AT * self.executions
        if rebuilt:
            shared += REBUILD_SHIFT
        cooldown = COOLDOWNS[int(self.rng.integers(0, len(COOLDOWNS)))]
        runs = tuple(self.run(number, index + 1, kind, shared, rebuilt) for index, kind in enumerate(kinds))
        return HistoryExecution(number, cooldown, runs)

    def run(self, execution: int, number: int, kind: Kind, shared: float, rebuilt: bool) -> HistoryRun:
        # A heavy-tailed run factor: Student-t with four degrees of freedom, scaled.
        factor = float(self.rng.standard_t(4)) * RUN_SCALE
        steps = kind.workload["steps"]
        mean_step = math.exp(_log_median_step(kind) + shared + factor)
        times = self.step_times(kind, mean_step, steps)
        scale_p, scale_f = kind.pixels / P0, kind.frames / (DT_F0 if kind.backend == DRAW_THINGS else H3_F0)
        stages: list[HistoryStage] = []
        cursor = 0.0

        def add(name: str, seconds: float, source: str = CHILD, **extra: Any) -> None:
            nonlocal cursor
            stages.append(HistoryStage(name, source, round(cursor, 6), round(cursor + seconds, 6), **extra))
            cursor += seconds

        add("prepare", self.lognormal(0.9, 0.25), FRAMECONTROL)
        if kind.backend == DRAW_THINGS:
            add("starting", self.lognormal(7.4, 0.05))
            add("processing", self.lognormal(6.5 * scale_p**0.3, 0.1))
            self.sampling(stages, "sampling", cursor, times)
            cursor = stages[-1].end
            add("finishing", self.lognormal(11.0 * scale_p**0.8 * scale_f**0.8, 0.06))
            tail = (("verify", 0.6), ("tag", 0.4), ("last_frame", 1.6)) + ((("correct", 28.0 * scale_f * scale_p**0.5), ("drift", 4.0)) if kind.correct else ()) + (("measure", 0.5),)
        else:
            for name, seconds, counter in self.h3_setup(kind):
                add(name, self.lognormal(seconds, 0.06), done=counter, total=counter)
            self.sampling(stages, "denoise", cursor, times)
            cursor = stages[-1].end
            add("audio_decode", self.lognormal(6.0 * scale_f, 0.06), done=6, total=6)
            add("video_load", self.lognormal(1.3, 0.06), done=36, total=36)
            frames = kind.frames
            ffmpeg_seconds = self.lognormal(100.0 * scale_f * scale_p**0.5, 0.06)
            ticks = np.cumsum(self.rng.uniform(0.9, 1.1, frames))
            marks = tuple((round(cursor + float(tick) / float(ticks[-1]) * ffmpeg_seconds, 6), index + 1) for index, tick in enumerate(ticks))
            add("ffmpeg", ffmpeg_seconds, done=frames, total=frames, unit="frames", marks=((round(cursor, 6), 0), *marks))
            tail = (("verify", 1.0), ("promote", 0.4), ("last_frame", 0.5)) + ((("correct", 42.0 * scale_f**0.8),) if kind.correct else ())
        for name, seconds in tail:
            add(name, self.lognormal(seconds, 0.06), FRAMECONTROL)
        if self.rng.random() < 0.3:
            add("unaccounted", round(float(self.rng.uniform(0.05, 0.4)), 6), FRAMECONTROL)
        environment = {"model": kind.model, "executable_bytes": 4_000_000 + (50_000 if rebuilt else 0), "executable_mtime": 1.7e9 + (4e5 if rebuilt else 0), "load_start": [1.0, 1.0, 1.0]}
        return HistoryRun(execution, number, "live", kind.backend, kind.pipeline, kind.workload, environment, tuple(stages))

    def step_times(self, kind: Kind, mean_step: float, steps: int) -> np.ndarray:
        position = (np.arange(steps) + 0.5) / steps
        if kind.backend == DRAW_THINGS:
            # Rising in seven runs of eight, as measured, and falling in the eighth.
            slope = 0.15 + self.normal(0.04) if self.rng.random() > 1 / 8 else -0.10 + self.normal(0.03)
        else:
            slope = self.normal(0.08)
        shape = np.exp(slope * (position - 0.5))
        times = mean_step * shape / shape.mean() * np.exp(self.rng.normal(0.0, STEP_NOISE, steps))
        if kind.backend == H3:
            times[0] *= float(np.clip(self.rng.normal(0.74, 0.12), 0.4, 1.0))
        if self.rng.random() < PAUSE_RATE and steps > 3:
            times[int(self.rng.integers(1, steps - 1))] *= float(self.rng.uniform(4.0, 15.0))
        return times

    @staticmethod
    def sampling(stages: list[HistoryStage], name: str, start: float, times: np.ndarray) -> None:
        """The iterative stage and its marks: the time each step began, 0 done at the stage's start, and all done at its end."""
        offsets = start + np.concatenate(([0.0], np.cumsum(times)))
        steps = len(times)
        marks = tuple((round(float(offsets[index]), 6), index) for index in range(steps + 1))
        stages.append(HistoryStage(name, CHILD, round(start, 6), round(float(offsets[-1]), 6), steps, steps, "steps", marks))

    def h3_setup(self, kind: Kind) -> list[tuple[str, float, int]]:
        phases = [("tokenize", 0.2, 1)]
        if kind.pipeline == "i2av":
            phases.append(("video_encode", 1.2, 9))
        if kind.pipeline == "r2av":
            phases.append(("vision", 1.0, 1))
        phases += [("text_encode", 4.3, 50), ("refine_text", 2.3, 1), ("adaln", 1.2, 2), ("load_core", 9.0, 50)]
        return phases


def synthetic_history(seed: int = 20261016, *, executions: int = 600) -> SyntheticHistory:
    """The history ``executions`` jobs left, in the order they ran."""
    return _Generator(seed, executions).build()
