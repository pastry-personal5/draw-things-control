"""The ring's entries: a complete live run reduced to numbers and fixed labels, so the file that holds them can carry no prompt,
path, log line, output name, or model filename.

An entry is rebuilt as a ``HistoryRun`` (the shape the fit reads) whose model is an opaque fingerprint. ``valid_entry`` is the
strict recursive whitelist: an entry with any other key, type, or label is rejected whole."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from typing import Any

from framecontrol.jobs.timing.features import FRAMECONTROL, IGNORED_KNOBS, HistoryRun, HistoryStage
from framecontrol.jobs.timing.model import MAX_MARKS, SOURCE_CHILD, thin_marks
from framecontrol.jobs.timing.parsers import H3_STAGES

RING_SCHEMA = 1
MAX_RING_MARKS = 120
MAX_RING_STAGES = 80
FINGERPRINT_LENGTH = 16
PIPELINES = frozenset({"i2i", "t2v", "i2v", "i2av", "r2av"})
REFERENCE_KINDS = frozenset({"image", "video", "silent_video", "audio", "video_audio"})
REFERENCE_IMAGE_SIZES = frozenset({"match", "max"})
UNITS = frozenset({"steps", "frames"})
SOURCES = frozenset({SOURCE_CHILD, FRAMECONTROL})
# The labels a stage can have: every one the parsers and Framecontrol's own timer produce; anything else is ``other``.
STAGE_LABELS = frozenset({*H3_STAGES.values(), "starting", "processing", "sampling", "finishing", "check_input", "convert", "copy_raw", "correct", "drift", "handoff_copy", "last_frame", "measure", "prepare_input", "promote", "stage_references", "tag", "verify", "prepare", "unaccounted", "other"})

_NUMBER_KEYS = frozenset({"width", "height", "pixels", "frames", "steps", "seconds", "guidance_scale", "strength", "shift", "reference_count", "render_width", "render_height", "denoise_reuse", "dit_layers", "core_reuse"})
_FLAG_KEYS = frozenset({"token_reduction", "ssd_streaming", "int8_row_fc2", "reference_rope", "color_corrects"})
_KNOB_NAME_LENGTH = 32
ENTRY_KEYS = frozenset({"v", "e", "r", "b", "p", "w", "m", "x", "o", "l", "s"})
WORKLOAD_KEYS = _NUMBER_KEYS | _FLAG_KEYS | {"pipeline", "mode", "reference_image_size", "references", "compatibility", "knobs"}
ENVIRONMENT_KEYS = frozenset({"bytes", "mtime"})
STAGE_KEYS = ("l", "c", "a", "z", "d", "t", "u", "k")


def fingerprint(model: Any) -> str:
    """An opaque, stable stand-in for a model's identity (truncated SHA-256), used only for equality."""
    return hashlib.sha256(str(model).encode("utf-8", "replace")).hexdigest()[:FINGERPRINT_LENGTH]


def signature_id(run_signature: Any) -> str:
    """A short stable name for a signature, the key of a per-signature summary in the calibration files."""
    return hashlib.sha1(repr(run_signature).encode("utf-8", "replace")).hexdigest()[:12]  # noqa: S324 (a name, not a secret)


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _load(value: Any) -> list[float] | None:
    """The host's 1, 5 and 15 minute load averages at the run's start, or None where it gave none."""
    if isinstance(value, list | tuple) and len(value) == 3 and all(_number(item) and 0 <= item < 1e6 for item in value):
        return [round(float(item), 3) for item in value]
    return None


def _knob_name(name: Any) -> bool:
    return isinstance(name, str) and 0 < len(name) <= _KNOB_NAME_LENGTH and name[0].isalpha() and all(char.islower() or char.isdigit() or char == "_" for char in name) and name not in IGNORED_KNOBS


def _workload(source: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in source.items():
        if key in _NUMBER_KEYS and _number(value):
            result[key] = value
        elif key in _FLAG_KEYS and isinstance(value, bool):
            result[key] = value
        elif key in ("pipeline", "mode") and isinstance(value, str) and value in PIPELINES:
            result[key] = value
        elif key == "reference_image_size" and isinstance(value, str) and value in REFERENCE_IMAGE_SIZES:
            result[key] = value
        elif key == "references" and isinstance(value, Mapping):
            result[key] = {kind: count for kind, count in value.items() if kind in REFERENCE_KINDS and isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 1000}
        elif key == "compatibility" and (value is True or (isinstance(value, Mapping) and any(value.values()))):
            result[key] = True
        elif key == "knobs" and isinstance(value, Mapping):
            knobs = {name: item for name, item in value.items() if _knob_name(name) and (_number(item) or isinstance(item, bool))}
            if knobs:
                result[key] = knobs
    return result


def sanitized_workload(workload: Mapping[str, Any]) -> dict[str, Any]:
    """A planned workload as the ring would record it, so a signature can be matched against a summary."""
    return _workload(workload)


def ring_entry(run: HistoryRun) -> dict[str, Any] | None:
    """The entry for a complete live run, or None for one the ring does not take (a log import, a pipeline or backend it
    cannot name, a run with no stages)."""
    if run.fidelity != "live" or run.pipeline not in PIPELINES or not run.stages:
        return None
    stages = []
    for stage in run.counted_stages[:MAX_RING_STAGES]:
        if stage.source not in SOURCES or not math.isfinite(stage.start) or not math.isfinite(stage.end):
            continue
        label = stage.stage if stage.stage in STAGE_LABELS else "other"
        marks = [[round(offset, 3), done] for offset, done in thin_marks(stage.marks, MAX_RING_MARKS)]
        stages.append([label, stage.source, round(stage.start, 3), round(stage.end, 3), stage.done, stage.total, stage.unit if stage.unit in UNITS else None, marks, bool(stage.uncertain)])
    if not stages:
        return None
    environment = run.environment
    entry: dict[str, Any] = {
        "v": RING_SCHEMA,
        "e": run.execution,
        "r": run.run,
        "b": run.backend,
        "p": run.pipeline,
        "w": _workload(run.workload),
        "m": fingerprint(environment.get("model")),
        "x": {key: environment[name] for key, name in (("bytes", "executable_bytes"), ("mtime", "executable_mtime")) if _number(environment.get(name))},
        "o": run.output_frames if isinstance(run.output_frames, int) and not isinstance(run.output_frames, bool) else None,
        "l": _load(environment.get("load_start")),
        "s": stages,
    }
    return entry if valid_entry(entry, {run.backend}) else None


def valid_entry(entry: Any, backends: set[str] | frozenset[str] | None = None) -> bool:
    """Whether ``entry`` is exactly what ``ring_entry`` makes: no other key, type, or label anywhere in it."""
    try:
        return _valid(entry, backends)
    except (TypeError, ValueError, KeyError):
        return False


def _valid(entry: Any, backends: set[str] | frozenset[str] | None) -> bool:
    if not isinstance(entry, dict) or set(entry) != ENTRY_KEYS or entry["v"] != RING_SCHEMA:
        return False
    if not all(isinstance(entry[key], int) and not isinstance(entry[key], bool) and entry[key] >= 0 for key in ("e", "r")):
        return False
    backend = entry["b"]
    if not isinstance(backend, str) or not 0 < len(backend) <= 40 or not all(char.islower() or char.isdigit() or char == "-" for char in backend) or (backends is not None and backend not in backends):
        return False
    if not isinstance(entry["p"], str) or entry["p"] not in PIPELINES or not isinstance(entry["m"], str) or len(entry["m"]) != FINGERPRINT_LENGTH or any(char not in "0123456789abcdef" for char in entry["m"]):
        return False
    if entry["o"] is not None and not (isinstance(entry["o"], int) and not isinstance(entry["o"], bool)):
        return False
    if entry["l"] is not None and _load(entry["l"]) != entry["l"]:
        return False
    if not _valid_workload(entry["w"]) or not isinstance(entry["x"], dict) or not set(entry["x"]) <= ENVIRONMENT_KEYS or not all(_number(value) for value in entry["x"].values()):
        return False
    stages = entry["s"]
    return isinstance(stages, list) and 0 < len(stages) <= MAX_RING_STAGES and all(_valid_stage(stage) for stage in stages)


def _valid_workload(workload: Any) -> bool:
    if not isinstance(workload, dict) or not set(workload) <= WORKLOAD_KEYS:
        return False
    return _workload(workload) == workload


def _valid_stage(stage: Any) -> bool:
    if not isinstance(stage, list) or len(stage) != 9:
        return False
    label, source, start, end, done, total, unit, marks, uncertain = stage
    if not isinstance(label, str) or label not in STAGE_LABELS or not isinstance(source, str) or source not in SOURCES or not _number(start) or not _number(end) or not isinstance(uncertain, bool):
        return False
    if any(value is not None and not (isinstance(value, int) and not isinstance(value, bool)) for value in (done, total)) or (unit is not None and (not isinstance(unit, str) or unit not in UNITS)):
        return False
    return isinstance(marks, list) and len(marks) <= MAX_RING_MARKS and all(isinstance(mark, list) and len(mark) == 2 and _number(mark[0]) and isinstance(mark[1], int) and not isinstance(mark[1], bool) for mark in marks)


def history_run(entry: Mapping[str, Any]) -> HistoryRun:
    """The run an entry records, ready for the fit. Only a valid entry may be passed."""
    stages = tuple(HistoryStage(label, source, float(start), float(end), done, total, unit, tuple((float(offset), int(count)) for offset, count in marks), bool(uncertain)) for label, source, start, end, done, total, unit, marks, uncertain in entry["s"])
    environment: dict[str, Any] = {"model": entry["m"]}
    environment.update({name: entry["x"][key] for key, name in (("bytes", "executable_bytes"), ("mtime", "executable_mtime")) if key in entry["x"]})
    if entry["l"] is not None:
        environment["load_start"] = list(entry["l"])
    return HistoryRun(entry["e"], entry["r"], "live", entry["b"], entry["p"], dict(entry["w"]), environment, stages, entry["o"])


__all__ = ["ENTRY_KEYS", "MAX_MARKS", "WORKLOAD_KEYS", "fingerprint", "history_run", "ring_entry", "sanitized_workload", "signature_id", "valid_entry"]
