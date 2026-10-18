"""The learned log terms of a step time for one workload: what ``StepModel.rate`` adds to its power law. The fit builds its
columns here and the forecast prices with the same, so the two cannot differ."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from framecontrol.jobs.timing.knobs import StepModel

# Features that are not run knobs: the structural columns the step model has terms of its own for.
STRUCTURAL = frozenset({"log_pixels", "log_frames", "log_refs", "log_render_pixels"})
KNOB_PREFIX = "knob:"


def size_key(workload: Mapping[str, Any]) -> str | None:
    """``WxH`` for the workload's exact size, the key of a size offset."""
    width, height = workload.get("width"), workload.get("height")
    if isinstance(width, int | float) and isinstance(height, int | float) and not isinstance(width, bool) and not isinstance(height, bool) and width > 0 and height > 0:
        return f"{int(width)}x{int(height)}"
    return None


def columns(workload: Mapping[str, Any], features: Mapping[str, float], reference: tuple[int, int]) -> dict[str, float]:
    """What a workload gives the step model's terms: log pixels and frames away from the reference (the exponents' columns),
    the log aspect, the render size relative to the output's, the log of one plus the references, and each run knob under
    ``knob:`` and its feature name. A term the workload does not have is absent, which the model reads as zero."""
    values: dict[str, float] = {}
    if "log_pixels" in features:
        values["pixel_exponent"] = features["log_pixels"] - math.log(reference[0])
    if "log_frames" in features:
        values["frame_exponent"] = features["log_frames"] - math.log(reference[1])
    width, height = workload.get("width"), workload.get("height")
    if isinstance(width, int | float) and isinstance(height, int | float) and not isinstance(width, bool) and not isinstance(height, bool) and width > 0 and height > 0:
        values["aspect"] = math.log(width / height)
    if "log_render_pixels" in features and "log_pixels" in features:
        values["render_exponent"] = features["log_render_pixels"] - features["log_pixels"]
    if "log_refs" in features:
        values["reference_exponent"] = features["log_refs"]
    for name, value in features.items():
        if name not in STRUCTURAL:
            values[f"{KNOB_PREFIX}{name}"] = value
    return values


def learned_terms(step: StepModel, workload: Mapping[str, Any], features: Mapping[str, float]) -> float:
    """The sum of the learned log terms (aspect, render size, references, knobs, and the exact size's offset) for a workload."""
    given = columns(workload, features, (step.pixels, step.frames))
    total = step.aspect * given.get("aspect", 0.0) + step.render_exponent * given.get("render_exponent", 0.0) + step.reference_exponent * given.get("reference_exponent", 0.0)
    for name, effect in step.knobs.items():
        total += effect * given.get(f"{KNOB_PREFIX}{name}", 0.0)
    key = size_key(workload)
    if key is not None:
        total += step.sizes.get(key, 0.0)
    return total


def unseen_knobs(observed: Mapping[str, tuple[float, ...]], baseline: Mapping[str, float], features: Mapping[str, float]) -> tuple[str, ...]:
    """The knobs a workload sets to a value never observed and different from the factory baseline's, by feature name. A knob
    with no observation at all is unseen too when it takes a nonzero value."""
    names = []
    for name, value in features.items():
        if name in STRUCTURAL:
            continue
        seen = observed.get(name)
        if seen is not None and any(math.isclose(value, item, rel_tol=1e-6, abs_tol=1e-6) for item in seen):
            continue
        if math.isclose(value, baseline.get(name, 0.0), rel_tol=1e-6, abs_tol=1e-6):
            continue
        names.append(name)
    return tuple(sorted(names))
