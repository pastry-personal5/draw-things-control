"""Factory timing evidence used before enough independent executions qualify for a history fit."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from framecontrol.core.backend_ids import DRAW_THINGS_BACKEND, H3_BACKEND


@dataclass(frozen=True)
class FactoryStage:
    key: str
    name: str
    source: str
    seconds: float
    iterative: bool = False


@dataclass(frozen=True)
class DrawThingsFactoryPoint:
    """E0053's two runs: one execution, so a narrow workload match and a wide prior spread."""

    backend: str = DRAW_THINGS_BACKEND
    pipeline: str = "i2v"
    model: str = "wan_v2.2_a14b_hne_i2v_i8x.ckpt"
    executable_bytes: int = 160477120
    executable_mtime: float = 1790314797.2408893
    width: int = 384
    height: int = 768
    frames: int = 33
    steps: int = 25
    guidance_scale: float = 5.0
    strength: float = 1.0
    shift: float = 3.99
    color_corrects: bool = True
    # Geometric mean of the two rates after their first gap: 13.428 and 13.919 s.
    step_seconds: float = 13.6710
    # Their fitted within-stage slopes were 0.240 and 0.170; uncertainty stays broad for one execution.
    slope: float = 0.205
    spread: float = 0.25
    first_ratio: float | None = None
    stage_factor: float = 0.99758
    step_noise: float = 0.066
    slope_sd: float = 0.10
    first_sd: float = 0.10
    tau: float = 0.10
    rho: float = 0.5
    pause_share: float = 0.0
    other_sd: float = 20.0
    stages: tuple[FactoryStage, ...] = (
        FactoryStage("prepare", "prepare", "framecontrol", 0.001),
        FactoryStage("starting", "starting", "child", 3.14),
        FactoryStage("processing", "processing", "child", 4.87),
        FactoryStage("sampling", "sampling", "child", 0.0, True),
        FactoryStage("finishing", "finishing", "child", 4.64),
        FactoryStage("verify", "verify", "framecontrol", 0.148),
        FactoryStage("tag", "tag", "framecontrol", 0.004),
        FactoryStage("verify#2", "verify", "framecontrol", 0.039),
        FactoryStage("last_frame", "last_frame", "framecontrol", 0.578),
        FactoryStage("correct", "correct", "framecontrol", 6.847),
        FactoryStage("verify#3", "verify", "framecontrol", 0.005),
        FactoryStage("measure", "measure", "framecontrol", 0.031),
    )

    def matches(self, backend: str, pipeline: str | None, workload: Mapping[str, Any], environment: Mapping[str, Any]) -> bool:
        expected = {"width": self.width, "height": self.height, "frames": self.frames, "steps": self.steps, "guidance_scale": self.guidance_scale, "strength": self.strength, "shift": self.shift, "color_corrects": self.color_corrects, "mode": self.pipeline}
        return backend == self.backend and pipeline == self.pipeline and environment.get("model") == self.model and environment.get("executable_bytes") == self.executable_bytes and environment.get("executable_mtime") == self.executable_mtime and all(workload.get(key) == value for key, value in expected.items())


DRAW_THINGS_I8X_E0053 = DrawThingsFactoryPoint()


@dataclass(frozen=True)
class H3FactoryPoint:
    """E0051 and E0054: two independent executions at one overridden 15-second r2av workload."""

    backend: str = H3_BACKEND
    pipeline: str = "r2av"
    model: str = "MiniMax-H3"
    executable_bytes: int = 558688
    executable_mtime: float = 1791736910.7589593
    width: int = 832
    height: int = 448
    frames: int = 362
    steps: int = 30
    # The step-series estimator excludes the first gap; 141.283 and 136.102 s were observed.
    step_seconds: float = 138.6683
    slope: float = 0.1685
    spread: float = 0.25
    first_ratio: float | None = 0.798
    stage_factor: float = 0.99334
    step_noise: float = 0.055
    slope_sd: float = 0.12
    first_sd: float = 0.15
    tau: float = 0.10
    rho: float = 0.5
    pause_share: float = 0.0
    other_sd: float = 20.0
    stages: tuple[FactoryStage, ...] = (
        FactoryStage("prepare", "prepare", "framecontrol", 0.001),
        FactoryStage("tokenize", "tokenize", "child", 0.203),
        FactoryStage("video_encode", "video_encode", "child", 2.413),
        FactoryStage("video_encode#2", "video_encode", "child", 2.369),
        FactoryStage("video_encode#3", "video_encode", "child", 2.815),
        FactoryStage("vision", "vision", "child", 0.266),
        FactoryStage("vision#2", "vision", "child", 0.225),
        FactoryStage("vision#3", "vision", "child", 0.539),
        FactoryStage("text_encode", "text_encode", "child", 9.653),
        FactoryStage("refine_text", "refine_text", "child", 0.215),
        FactoryStage("adaln", "adaln", "child", 4.249),
        FactoryStage("load_core", "load_core", "child", 7.632),
        FactoryStage("denoise", "denoise", "child", 0.0, True),
        FactoryStage("audio_decode", "audio_decode", "child", 0.887),
        FactoryStage("video_load", "video_load", "child", 1.091),
        FactoryStage("ffmpeg", "ffmpeg", "child", 189.977),
        FactoryStage("verify", "verify", "framecontrol", 0.468),
        FactoryStage("verify#2", "verify", "framecontrol", 0.074),
        FactoryStage("convert", "convert", "framecontrol", 1.266),
        FactoryStage("drift", "drift", "framecontrol", 0.179),
        FactoryStage("promote", "promote", "framecontrol", 0.001),
    )

    def matches(self, backend: str, pipeline: str | None, workload: Mapping[str, Any], environment: Mapping[str, Any]) -> bool:
        expected = {"pipeline": self.pipeline, "width": self.width, "height": self.height, "frames": self.frames, "seconds": self.frames / 24, "steps": self.steps, "denoise_reuse": 1, "dit_layers": 50, "core_reuse": 1, "token_reduction": False, "ssd_streaming": False, "int8_row_fc2": False, "reference_rope": False, "reference_image_size": "match", "render_width": None, "render_height": None, "references": {"image": 3}, "reference_count": 3}
        knobs = workload.get("knobs")
        return backend == self.backend and pipeline == self.pipeline and environment.get("model") == self.model and environment.get("executable_bytes") == self.executable_bytes and environment.get("executable_mtime") == self.executable_mtime and all(workload.get(key) == value for key, value in expected.items()) and workload.get("color_corrects") is not True and "compatibility" not in workload and isinstance(knobs, Mapping) and knobs.get("seconds") == 15 and knobs.get("requested_frames") == 360 and knobs.get("effective_frames") == self.frames and knobs.get("effective_seconds") == self.frames / 24 and knobs.get("reference_pair") == "three-subjects"


H3_R2AV_E0051_E0054 = H3FactoryPoint()


def draw_things_factory_point(backend: str, pipeline: str | None, workload: Mapping[str, Any], environment: Mapping[str, Any]) -> DrawThingsFactoryPoint | None:
    """Only use E0053 where all recorded work and color controls agree."""
    return DRAW_THINGS_I8X_E0053 if DRAW_THINGS_I8X_E0053.matches(backend, pipeline, workload, environment) else None


def factory_point(backend: str, pipeline: str | None, workload: Mapping[str, Any], environment: Mapping[str, Any]) -> DrawThingsFactoryPoint | H3FactoryPoint | None:
    """A measured point only where the run's resolved workload and build agree."""
    if backend == DRAW_THINGS_BACKEND:
        return draw_things_factory_point(backend, pipeline, workload, environment)
    if backend == H3_BACKEND and H3_R2AV_E0051_E0054.matches(backend, pipeline, workload, environment):
        return H3_R2AV_E0051_E0054
    return None


def heuristic_anchor(backend: str) -> DrawThingsFactoryPoint | H3FactoryPoint | None:
    """A measured backend rate to scale for a cold start, with uncertainty supplied by the caller."""
    if backend == DRAW_THINGS_BACKEND:
        return DRAW_THINGS_I8X_E0053
    if backend == H3_BACKEND:
        return H3_R2AV_E0051_E0054
    return None
