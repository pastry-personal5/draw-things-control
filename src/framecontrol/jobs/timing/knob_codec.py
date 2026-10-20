"""A backend's calibration, and the tunables, as the mappings the YAML files hold, and back.

Reading is per key: a value that is missing or invalid leaves the factory value in its place and costs its siblings nothing, and
every one that was refused is named in the problems returned. ``flatten`` lists the numbers a hand edit can pin, each under the
path ``pipelines/<pipeline>/step/seconds`` (``/`` because a knob's name may hold a dot)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from framecontrol.jobs.timing.knobs import SEARCHED, BackendCalibration, FactoryStage, PipelineCalibration, Spreads, StepModel, Summary, Tunables, tunables_from_mapping, tunables_to_mapping
from framecontrol.jobs.timing.ring import SOURCES, STAGE_LABELS

SCHEMA = 1
MAX_NOTES = 4000
MAX_SIGNATURES = 40
MAX_OBSERVED_VALUES = 20
MAX_OBSERVED_NAMES = 60
MAX_STAGES = 80
MAX_SIZES = 200
MAX_KNOBS = 100
# What a machine rewrite may change in each file; everything else under these is the owner's.
READ_ONLY_TOP = frozenset({"schema", "updated_at", "evidence", "baseline", "backend", "anchor"})
READ_ONLY_PIPELINE = frozenset({"source", "evidence", "updated_at", "observed", "signatures"})

STEP_BOUNDS: dict[str, tuple[float, float]] = {"pixels": (1, 1e9), "frames": (1, 1e5), "seconds": (1e-3, 1e5), "pixel_exponent": (0.0, 3.0), "frame_exponent": (0.0, 3.0), "shift": (-3.0, 3.0), "aspect": (-3.0, 3.0), "render_exponent": (-3.0, 3.0), "reference_exponent": (-3.0, 3.0)}
KNOB_BOUNDS = (-4.0, 4.0)
SIZE_BOUNDS = (-2.0, 2.0)
SPREAD_BOUNDS: dict[str, tuple[float, float]] = {"tau": (0.02, 1.0), "rho": (0.05, 0.95), "slope": (-2.0, 2.0), "slope_sd": (0.01, 2.0), "first_ratio": (0.05, 20.0), "first_sd": (0.0, 2.0), "step_noise": (0.01, 1.0), "stage_factor": (0.2, 3.0), "other_sd": (0.0, 1e5), "pause_share": (0.0, 2.0)}
SPREAD_NULLABLE = frozenset({"first_ratio"})
PIPELINE_SPREAD_BOUNDS = (0.05, 3.0)
STAGE_BOUNDS: dict[str, tuple[float, float]] = {"seconds": (0.0, 1e6), "pixel_exponent": (0.0, 3.0), "frame_exponent": (0.0, 3.0), "sigma": (0.01, 3.0)}
EVIDENCE_KEYS = ("runs", "executions", "checkpoint")


def schema_state(raw: Any) -> str:
    """``ok`` for the version this code writes, ``older`` for one it migrates, ``newer`` for one it only reads, else ``invalid``."""
    if not isinstance(raw, Mapping):
        return "invalid"
    version = raw.get("schema")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return "invalid"
    return "ok" if version == SCHEMA else "older" if version < SCHEMA else "newer"


def _real(value: Any, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or not low <= value <= high:
        return None
    return float(value)


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**9 else None


# ------------------------------------------------------------------ tuning file


def tunables_from_file(raw: Mapping[str, Any], base: Tunables | None = None) -> tuple[Tunables, list[str]]:
    """The tunables a ``tuning.yaml`` mapping holds: ``tunables`` (the searched ones) and ``floors`` (the rest)."""
    merged: dict[str, Any] = {}
    problems: list[str] = []
    for section in ("tunables", "floors"):
        values = raw.get(section)
        if values is None:
            continue
        if not isinstance(values, Mapping):
            problems.append(f"{section}: expected a mapping")
            continue
        merged.update(values)
    result, found = tunables_from_mapping(merged, base)
    return result, [*problems, *found]


def tunables_to_file(tunables: Tunables) -> dict[str, Any]:
    flat = tunables_to_mapping(tunables)
    return {"schema": SCHEMA, "tunables": {name: flat[name] for name in SEARCHED}, "floors": {name: value for name, value in flat.items() if name not in SEARCHED}}


# ------------------------------------------------------------------ calibration file


def backend_to_mapping(calibration: BackendCalibration) -> dict[str, Any]:
    """The file form of a backend's calibration, pipelines in the order the calibration lists them."""
    return {
        "schema": SCHEMA,
        "backend": calibration.backend,
        "updated_at": calibration.updated_at,
        "notes": calibration.notes,
        "anchor": calibration.anchor,
        "baseline": dict(calibration.baseline),
        "pipelines": {name: pipeline_to_mapping(pipeline) for name, pipeline in calibration.pipelines.items()},
    }


def pipeline_to_mapping(pipeline: PipelineCalibration) -> dict[str, Any]:
    step = pipeline.step
    spreads = pipeline.spreads
    return {
        "source": pipeline.source,
        "evidence": {"runs": pipeline.runs, "executions": pipeline.executions, "checkpoint": pipeline.checkpoint},
        "updated_at": pipeline.updated_at,
        "step": {"pixels": step.pixels, "frames": step.frames, "seconds": step.seconds, "pixel_exponent": step.pixel_exponent, "frame_exponent": step.frame_exponent, "shift": step.shift, "aspect": step.aspect, "render_exponent": step.render_exponent, "reference_exponent": step.reference_exponent, "knobs": dict(step.knobs), "sizes": dict(step.sizes)},
        "spreads": {"tau": spreads.tau, "rho": spreads.rho, "slope": spreads.slope, "slope_sd": spreads.slope_sd, "first_ratio": spreads.first_ratio, "first_sd": spreads.first_sd, "step_noise": spreads.step_noise, "stage_factor": spreads.stage_factor, "other_sd": spreads.other_sd, "pause_share": spreads.pause_share},
        "spread": pipeline.spread,
        "stages": {stage.key: {"name": stage.name, "source": stage.source, "seconds": stage.seconds, "iterative": stage.iterative, "pixel_exponent": stage.pixel_exponent, "frame_exponent": stage.frame_exponent, "sigma": stage.sigma} for stage in pipeline.stages},
        "observed": {name: list(values) for name, values in pipeline.observed.items()},
        "signatures": {name: {"count": summary.count, "executions": summary.executions, "last_seen": summary.last_seen, "step_seconds": summary.step_seconds, "stages": dict(summary.stages)} for name, summary in pipeline.summaries.items()},
    }


def backend_from_mapping(raw: Mapping[str, Any], factory: BackendCalibration) -> tuple[BackendCalibration, list[str]]:
    """``factory`` with each valid value of ``raw`` applied; the problems name each one that was not."""
    problems: list[str] = []
    notes = raw.get("notes", "")
    if not isinstance(notes, str):
        problems.append("notes: expected text")
        notes = ""
    stamp = raw.get("updated_at")
    pipelines = dict(factory.pipelines)
    section = raw.get("pipelines")
    if section is not None and not isinstance(section, Mapping):
        problems.append("pipelines: expected a mapping")
        section = None
    anchor = factory.pipelines[factory.anchor]
    for name, value in (section or {}).items():
        if not isinstance(name, str) or not name.replace("_", "").isalnum() or len(name) > 20:
            problems.append(f"pipelines/{name}: not a pipeline name")
            continue
        if not isinstance(value, Mapping):
            problems.append(f"pipelines/{name}: expected a mapping")
            continue
        base = pipelines.get(name) or replace(anchor, pipeline=name, source="provisional", runs=0, executions=0, checkpoint=0, updated_at=None, observed={}, summaries={})
        pipelines[name] = _pipeline_from_mapping(name, value, base, problems)
    return replace(factory, pipelines=pipelines, notes=notes[:MAX_NOTES], updated_at=stamp if isinstance(stamp, str) else None), problems


def _pipeline_from_mapping(name: str, raw: Mapping[str, Any], base: PipelineCalibration, problems: list[str]) -> PipelineCalibration:
    where = f"pipelines/{name}"
    step = _step_from_mapping(raw.get("step"), base.step, problems, f"{where}/step") if raw.get("step") is not None else base.step
    spreads = _spreads_from_mapping(raw.get("spreads"), base.spreads, problems, f"{where}/spreads") if raw.get("spreads") is not None else base.spreads
    spread = base.spread
    if "spread" in raw:
        if raw["spread"] is None:
            spread = None
        elif (checked := _real(raw["spread"], *PIPELINE_SPREAD_BOUNDS)) is None:
            problems.append(f"{where}/spread: expected null or a number from {PIPELINE_SPREAD_BOUNDS[0]} to {PIPELINE_SPREAD_BOUNDS[1]}")
        else:
            spread = checked
    stages = _stages_from_mapping(raw.get("stages"), base.stages, problems, f"{where}/stages") if raw.get("stages") is not None else base.stages
    claimed = raw.get("source")
    source = claimed if isinstance(claimed, str) and claimed in ("factory", "provisional", "learned") else base.source
    evidence = raw.get("evidence")
    counts = {key: _count(evidence.get(key)) if isinstance(evidence, Mapping) else None for key in EVIDENCE_KEYS}
    stamp = raw.get("updated_at")
    return PipelineCalibration(
        name,
        step,
        spreads,
        stages,
        spread,
        source,
        counts["runs"] if counts["runs"] is not None else base.runs,
        counts["executions"] if counts["executions"] is not None else base.executions,
        counts["checkpoint"] if counts["checkpoint"] is not None else base.checkpoint,
        stamp if isinstance(stamp, str) else base.updated_at,
        _observed_from_mapping(raw.get("observed"), base.observed),
        _signatures_from_mapping(raw.get("signatures"), base.summaries),
    )


def _step_from_mapping(raw: Any, base: StepModel, problems: list[str], where: str) -> StepModel:
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: expected a mapping")
        return base
    values: dict[str, Any] = {}
    for key, (low, high) in STEP_BOUNDS.items():
        if key not in raw:
            continue
        checked = _real(raw[key], low, high)
        if checked is None:
            problems.append(f"{where}/{key}: expected a number from {low:g} to {high:g}, got {raw[key]!r}")
        else:
            values[key] = int(checked) if key in ("pixels", "frames") else checked
    for key in raw:
        if key not in STEP_BOUNDS and key not in ("knobs", "sizes"):
            problems.append(f"{where}/{key}: not a step value")
    knobs = _effects(raw.get("knobs"), base.knobs, KNOB_BOUNDS, MAX_KNOBS, problems, f"{where}/knobs") if "knobs" in raw else base.knobs
    sizes = _effects(raw.get("sizes"), base.sizes, SIZE_BOUNDS, MAX_SIZES, problems, f"{where}/sizes", size_keys=True) if "sizes" in raw else base.sizes
    return replace(base, **values, knobs=knobs, sizes=sizes)


def _effects(raw: Any, base: Mapping[str, float], bounds: tuple[float, float], limit: int, problems: list[str], where: str, *, size_keys: bool = False) -> Mapping[str, float]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: expected a mapping")
        return base
    result: dict[str, float] = {}
    for name, value in list(raw.items())[:limit]:
        checked = _real(value, *bounds)
        if not isinstance(name, str) or not 0 < len(name) <= 64 or (size_keys and not _size_key(name)) or checked is None:
            problems.append(f"{where}/{name}: expected a number from {bounds[0]:g} to {bounds[1]:g}")
        else:
            result[name] = checked
    return result


def _size_key(name: str) -> bool:
    width, separator, height = name.partition("x")
    return separator == "x" and width.isdigit() and height.isdigit() and 0 < int(width) <= 100000 and 0 < int(height) <= 100000


def _spreads_from_mapping(raw: Any, base: Spreads, problems: list[str], where: str) -> Spreads:
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: expected a mapping")
        return base
    values: dict[str, Any] = {}
    for key, (low, high) in SPREAD_BOUNDS.items():
        if key not in raw:
            continue
        if raw[key] is None and key in SPREAD_NULLABLE:
            values[key] = None
            continue
        checked = _real(raw[key], low, high)
        if checked is None:
            problems.append(f"{where}/{key}: expected {'null or ' if key in SPREAD_NULLABLE else ''}a number from {low:g} to {high:g}, got {raw[key]!r}")
        else:
            values[key] = checked
    for key in raw:
        if key not in SPREAD_BOUNDS:
            problems.append(f"{where}/{key}: not a spread")
    return replace(base, **values)


def _stages_from_mapping(raw: Any, base: tuple[FactoryStage, ...], problems: list[str], where: str) -> tuple[FactoryStage, ...]:
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: expected a mapping")
        return base
    known = {stage.key: stage for stage in base}
    stages: list[FactoryStage] = []
    for key, value in list(raw.items())[:MAX_STAGES]:
        if not isinstance(key, str) or not 0 < len(key) <= 40 or not isinstance(value, Mapping):
            problems.append(f"{where}/{key}: expected a stage mapping")
            continue
        earlier = known.get(key)
        stage = _stage_from_mapping(key, value, earlier, problems, f"{where}/{key}")
        if stage is not None:
            stages.append(stage)
    return tuple(stages) if stages else base


def _stage_from_mapping(key: str, raw: Mapping[str, Any], base: FactoryStage | None, problems: list[str], where: str) -> FactoryStage | None:
    name = raw.get("name", base.name if base else key.partition("#")[0])
    source = raw.get("source", base.source if base else None)
    if not isinstance(name, str) or name not in STAGE_LABELS or not isinstance(source, str) or source not in SOURCES:
        problems.append(f"{where}: needs a known name and a source of child or framecontrol")
        return base
    iterative = raw.get("iterative", base.iterative if base else False)
    if not isinstance(iterative, bool):
        problems.append(f"{where}/iterative: expected true or false")
        iterative = base.iterative if base else False
    values: dict[str, Any] = {"seconds": base.seconds if base else 0.0, "pixel_exponent": base.pixel_exponent if base else 0.0, "frame_exponent": base.frame_exponent if base else 0.0, "sigma": base.sigma if base else None}
    for field, (low, high) in STAGE_BOUNDS.items():
        if field not in raw:
            continue
        if raw[field] is None and field == "sigma":
            values[field] = None
            continue
        checked = _real(raw[field], low, high)
        if checked is None:
            problems.append(f"{where}/{field}: expected a number from {low:g} to {high:g}, got {raw[field]!r}")
        else:
            values[field] = checked
    return FactoryStage(key, name, source, values["seconds"], iterative, values["pixel_exponent"], values["frame_exponent"], values["sigma"])


def _observed_from_mapping(raw: Any, base: Mapping[str, tuple[float, ...]]) -> Mapping[str, tuple[float, ...]]:
    if not isinstance(raw, Mapping):
        return base
    result: dict[str, tuple[float, ...]] = {}
    for name, values in list(raw.items())[:MAX_OBSERVED_NAMES]:
        if isinstance(name, str) and 0 < len(name) <= 64 and isinstance(values, list):
            result[name] = tuple(checked for value in values[:MAX_OBSERVED_VALUES] if (checked := _real(value, -1e12, 1e12)) is not None)
    return result


def _signatures_from_mapping(raw: Any, base: Mapping[str, Summary]) -> Mapping[str, Summary]:
    if not isinstance(raw, Mapping):
        return base
    result: dict[str, Summary] = {}
    for name, value in list(raw.items())[:MAX_SIGNATURES]:
        if not isinstance(name, str) or not 0 < len(name) <= 24 or not isinstance(value, Mapping):
            continue
        count, executions, last_seen = _count(value.get("count")), _count(value.get("executions")), _count(value.get("last_seen"))
        stages = value.get("stages")
        if count is None or executions is None or last_seen is None or not isinstance(stages, Mapping):
            continue
        seconds = {key: checked for key, item in list(stages.items())[:MAX_STAGES] if isinstance(key, str) and 0 < len(key) <= 40 and (checked := _real(item, 0.0, 1e6)) is not None}
        step = value.get("step_seconds")
        result[name] = Summary(count, last_seen, seconds, _real(step, 1e-3, 1e5) if step is not None else None, executions)
    return result


# ------------------------------------------------------------------ pins


def flatten(mapping: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Every scalar leaf under ``mapping`` that a hand edit can pin, by ``a/b/c`` path: the numbers, flags and texts the
    machine writes, not the read-only blocks (``evidence``, ``updated_at``, ``source``, ``observed``, ``signatures``, ``baseline``)."""
    leaves: dict[str, Any] = {}
    for key, value in mapping.items():
        if (not prefix and key in READ_ONLY_TOP) or (prefix.startswith("pipelines/") and prefix.count("/") == 2 and key in READ_ONLY_PIPELINE):
            continue
        path = f"{prefix}{key}"
        if isinstance(value, Mapping):
            leaves.update(flatten(value, f"{path}/"))
        elif isinstance(value, bool) or value is None or isinstance(value, int | float):
            leaves[path] = value
        elif isinstance(value, str) and key != "notes":
            leaves[path] = value
    return leaves


def read_only_paths(raw: Mapping[str, Any]) -> list[str]:
    """The read-only keys a mapping holds, for the warning when a hand edit touches them."""
    found = [key for key in raw if key in READ_ONLY_TOP and key not in ("schema", "backend", "anchor")]
    pipelines = raw.get("pipelines")
    if isinstance(pipelines, Mapping):
        found.extend(f"pipelines/{name}/{key}" for name, value in pipelines.items() if isinstance(value, Mapping) for key in value if key in READ_ONLY_PIPELINE)
    return found
