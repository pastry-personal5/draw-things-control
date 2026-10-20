"""The timing knob files as the forecast and the commands see them: loaded per key with factory fallbacks, hand edits found
as pins, and the operations the ``fc timing knobs`` commands run. ``load_knobs`` never raises; a file that cannot be used is
reported in ``issues`` and replaced by its newest valid compatible backup, else by the factory, and left untouched."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from framecontrol.core.backend_ids import KNOWN_BACKENDS
from framecontrol.core.errors import FramecontrolError
from framecontrol.core.paths import ProjectPaths
from framecontrol.jobs.timing.factory import factory_calibration, factory_tunables
from framecontrol.jobs.timing.knob_codec import backend_from_mapping, backend_to_mapping, flatten, pipeline_to_mapping, schema_state, tunables_from_file, tunables_to_file
from framecontrol.jobs.timing.knobs import KNOBS_FACTORY, KNOBS_LEARNED, KNOBS_PINNED, BackendCalibration, Calibration, PipelineCalibration, Tunables
from framecontrol.jobs.timing.ring import PIPELINES
from framecontrol.services.timing_knob_files import TUNING_FILE, VALIDATED_FILE, KnobFiles, Written, digest, dump_yaml, read_yaml

if TYPE_CHECKING:
    from framecontrol.services.knob_updater import UpdateReport

_MISSING = object()
EXAMPLE_SUFFIX = ".example.yaml"


def knob_files(paths: ProjectPaths) -> KnobFiles:
    return KnobFiles(paths.timing_data)


def backend_file(backend: str) -> str:
    return f"{backend}.yaml"


@dataclass(frozen=True)
class Issue:
    file: str
    message: str
    level: str = "warning"

    def __str__(self) -> str:
        return f"{self.file}: {self.message}"


@dataclass(frozen=True)
class FileState:
    """How one file was used: ``file`` (read as written), ``backup`` (its newest valid backup stood in), ``factory`` (none
    was usable or the file is absent). ``suspended`` means automatic writes to it are off until a person repairs it."""

    name: str
    origin: str
    suspended: bool = False
    detail: str | None = None
    pins: Mapping[str, Any] = field(default_factory=dict)
    mapping: Mapping[str, Any] | None = None
    present: bool = False
    # The file's digest as it was read (None when absent): what an update must still find when it replaces the file.
    digest: str | None = None


@dataclass(frozen=True)
class LoadedKnobs:
    tunables: Tunables
    calibration: Calibration
    issues: tuple[Issue, ...]
    files: Mapping[str, FileState]
    signature: tuple[Any, ...] = ()

    @property
    def suspended(self) -> frozenset[str]:
        return frozenset(name for name, state in self.files.items() if state.suspended)


def factory_loaded() -> LoadedKnobs:
    return LoadedKnobs(factory_tunables(), factory_calibration(), (), {})


# ------------------------------------------------------------------ readonly blocks


def extract_readonly(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """The blocks a hand edit cannot change: the top ``updated_at`` and each pipeline's ``source``, ``evidence`` and ``updated_at``."""
    pipelines = mapping.get("pipelines")
    result: dict[str, Any] = {"updated_at": mapping.get("updated_at"), "pipelines": {}}
    if isinstance(pipelines, Mapping):
        for name, value in pipelines.items():
            if isinstance(value, Mapping):
                result["pipelines"][name] = {key: value.get(key) for key in ("source", "evidence", "updated_at") if key in value}
    return result


def apply_readonly(mapping: Mapping[str, Any], recorded: Mapping[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """``mapping`` with the read-only blocks as the machine last wrote them; the blocks the edit changed."""
    result = json.loads(json.dumps(dict(mapping), default=str))
    changed: list[str] = []
    if not isinstance(recorded, Mapping):
        return result, changed
    if result.get("updated_at") != recorded.get("updated_at"):
        changed.append("updated_at")
        result["updated_at"] = recorded.get("updated_at")
    pipelines = result.get("pipelines")
    recorded_pipelines = recorded.get("pipelines")
    if isinstance(pipelines, dict) and isinstance(recorded_pipelines, Mapping):
        for name, value in pipelines.items():
            kept = recorded_pipelines.get(name)
            if not isinstance(value, dict) or not isinstance(kept, Mapping):
                continue
            for key in ("source", "evidence", "updated_at"):
                if key in kept and value.get(key) != kept[key]:
                    changed.append(f"pipelines/{name}/{key}")
                    value[key] = kept[key]
    return result, changed


# ------------------------------------------------------------------ pins


def detect_pins(valid: Mapping[str, Any], record: Mapping[str, Any] | None, factory_flat: Mapping[str, Any]) -> dict[str, Any]:
    """The values a person set: every valid value that differs from what the machine last wrote (else from the factory),
    and every one already pinned that is still in the file. A key the file no longer holds is not pinned."""
    baseline = record.get("values", {}) if isinstance(record, Mapping) else factory_flat
    kept = record.get("pins", {}) if isinstance(record, Mapping) else {}
    return {path: value for path, value in valid.items() if path in kept or value != baseline.get(path, _MISSING)}


def pins_of_state(state: FileState, pipeline: str) -> dict[str, Any]:
    """A pipeline's pins as paths inside it (``step/seconds``)."""
    prefix = f"pipelines/{pipeline}/"
    return {path[len(prefix) :]: value for path, value in state.pins.items() if path.startswith(prefix)}


# ------------------------------------------------------------------ loading


def _backup_candidates(files: KnobFiles, name: str) -> list[Path]:
    return list(reversed(files.backups_of(name)))


def _usable(raw: Any) -> bool:
    return isinstance(raw, Mapping) and schema_state(raw) in ("ok", "older")


def read_validated(files: KnobFiles) -> tuple[dict[str, Any], dict[str, PipelineCalibration], str | None]:
    """The approved baselines, or a valid backup with an error that suspends automatic approval."""
    empty: dict[str, Any] = {"schema": 1, "backends": {}}

    def parse(text: str) -> tuple[dict[str, Any], dict[str, PipelineCalibration]]:
        document = json.loads(text)
        if not isinstance(document, dict) or set(document) != {"schema", "backends"} or document.get("schema") != 1 or not isinstance(document.get("backends"), dict):
            raise ValueError("expected schema 1 and a backends mapping")
        baselines: dict[str, PipelineCalibration] = {}
        for backend, pipelines in document["backends"].items():
            if backend not in KNOWN_BACKENDS or not isinstance(pipelines, dict):
                raise ValueError(f"invalid backend {backend!r}")
            for pipeline, record in pipelines.items():
                if pipeline not in PIPELINES or not isinstance(record, dict) or set(record) != {"approved_at", "runs", "executions", "holdout_executions", "calibration", "tunables"} or not isinstance(record.get("calibration"), dict) or not isinstance(record.get("tunables"), dict):
                    raise ValueError(f"invalid approval for {backend}/{pipeline}")
                tunables, tuning_problems = tunables_from_file(record["tunables"])
                if schema_state(record["tunables"]) != "ok" or tuning_problems or tunables_to_file(tunables) != record["tunables"]:
                    raise ValueError(f"invalid tunables for {backend}/{pipeline}")
                parsed, problems = backend_from_mapping({"pipelines": {pipeline: record["calibration"]}}, factory_calibration().backends[backend])
                calibration = parsed.pipelines[pipeline]
                counts = (record.get("runs"), record.get("executions"), record.get("holdout_executions"))
                valid_counts = all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in counts)
                if problems or calibration.source != KNOBS_LEARNED or pipeline_to_mapping(calibration) != record["calibration"] or not isinstance(record.get("approved_at"), str) or record["approved_at"] != calibration.updated_at or not valid_counts or record["runs"] != calibration.runs or record["executions"] != calibration.executions or not 3 <= record["holdout_executions"] <= 8:
                    detail = "; ".join(problems[:2]) or "metadata or canonical calibration differs"
                    raise ValueError(f"invalid calibration for {backend}/{pipeline}: {detail}")
                baselines[f"{backend}/{pipeline}"] = calibration
        return document, baselines

    path = files.path_of(VALIDATED_FILE)
    if not path.exists():
        return (empty, {}, "is missing despite a recorded machine write; automatic approvals are suspended") if VALIDATED_FILE in files.read_written().files else (empty, {}, None)
    try:
        text = path.read_text(encoding="utf-8")
        recorded = files.read_written().files.get(VALIDATED_FILE)
        if not isinstance(recorded, dict) or recorded.get("digest") != digest(text):
            raise ValueError("digest differs from the last machine write")
        return *parse(text), None
    except (OSError, UnicodeDecodeError, ValueError, KeyError) as error:
        problem = f"cannot be used ({error}); automatic approvals are suspended"
    for backup in reversed(files.backups_of(VALIDATED_FILE)):
        try:
            document, baselines = parse(backup.read_text(encoding="utf-8"))
            return document, baselines, f"{problem}; using backup {backup.name} for comparison"
        except (OSError, UnicodeDecodeError, ValueError, KeyError):
            continue
    return empty, {}, problem


def _load_backend(files: KnobFiles, backend: str, written: Written, issues: list[Issue]) -> tuple[BackendCalibration, FileState]:
    name = backend_file(backend)
    factory = factory_calibration().backends[backend]
    seen = files.current_digest(name)
    raw, error = read_yaml(files.directory / name)
    present = raw is not None or error is not None
    state_label = None
    if raw is None and error is None:
        return factory, FileState(name, "factory", present=False, digest=seen)
    if error is None and isinstance(raw, Mapping) and schema_state(raw) == "newer":
        state_label = f"is from a newer version of Framecontrol (schema {raw.get('schema')})"
    elif error is None and not _usable(raw):
        state_label = "must be a mapping with a supported `schema:`"
    elif error is not None:
        state_label = error
    if state_label is not None:
        issues.append(Issue(name, state_label + "; automatic updates to it are off until it is repaired, rolled back, or reset", "error"))
        for backup in _backup_candidates(files, name):
            candidate, problem = read_yaml(backup)
            if problem is None and _usable(candidate):
                calibration, found = backend_from_mapping(candidate, factory)
                issues.extend(Issue(name, f"(backup {backup.name}) {text}") for text in found)
                return calibration, FileState(name, "backup", True, backup.name, {}, candidate, present, seen)
        return factory, FileState(name, "factory", True, state_label, {}, None, present, seen)
    assert isinstance(raw, Mapping)
    record = written.files.get(name)
    mapping, changed = apply_readonly(raw, record.get("readonly") if isinstance(record, Mapping) else None)
    for key in changed:
        issues.append(Issue(name, f"`{key}` is read-only; the edit is ignored", "warning"))
    calibration, problems = backend_from_mapping(mapping, factory)
    issues.extend(Issue(name, text) for text in problems)
    effective = flatten(backend_to_mapping(calibration))
    valid = {path: value for path, value in flatten(mapping).items() if path in effective and effective[path] == value}
    pins = detect_pins(valid, record if isinstance(record, Mapping) else None, flatten(backend_to_mapping(factory)))
    return calibration, FileState(name, "file", False, None, pins, mapping, True, seen)


def load_knobs(files: KnobFiles) -> LoadedKnobs:
    """The tunables and calibration the files give, each key valid or factory. Never raises."""
    try:
        return _load(files)
    except Exception:
        logger.exception("The timing knob files could not be read; the factory knobs stand")
        return replace(factory_loaded(), issues=(Issue("timing-data-analysis", "could not be read; the factory knobs are used", "error"),))


def _load(files: KnobFiles) -> LoadedKnobs:
    issues: list[Issue] = []
    written = files.read_written()
    signature = files.signature()
    tunables = factory_tunables()
    states: dict[str, FileState] = {}
    tuning_seen = files.current_digest(TUNING_FILE)
    raw, error = read_yaml(files.directory / TUNING_FILE)
    if raw is None and error is None:
        states[TUNING_FILE] = FileState(TUNING_FILE, "factory", digest=tuning_seen)
    else:
        source: Any = raw
        origin, suspended, detail = "file", False, None
        if error is not None or not _usable(raw):
            problem = error or ("is from a newer version of Framecontrol" if isinstance(raw, Mapping) and schema_state(raw) == "newer" else "must be a mapping with a supported `schema:`")
            issues.append(Issue(TUNING_FILE, problem + "; automatic writes to it are off", "error"))
            origin, suspended, detail, source = "factory", True, problem, None
            for backup in _backup_candidates(files, TUNING_FILE):
                candidate, bad = read_yaml(backup)
                if bad is None and _usable(candidate):
                    origin, detail, source = "backup", backup.name, candidate
                    break
        if isinstance(source, Mapping):
            tunables, problems = tunables_from_file(source)
            issues.extend(Issue(TUNING_FILE, text) for text in problems)
        pins: dict[str, Any] = {}
        if origin == "file" and isinstance(source, Mapping):
            effective = flatten(tunables_to_file(tunables))
            valid = {path: value for path, value in flatten(source).items() if path in effective and effective[path] == value}
            record = written.files.get(TUNING_FILE)
            pins = detect_pins(valid, record if isinstance(record, Mapping) else None, flatten(tunables_to_file(factory_tunables())))
        states[TUNING_FILE] = FileState(TUNING_FILE, origin, suspended, detail, pins, source if isinstance(source, Mapping) else None, True, tuning_seen)
    backends: dict[str, BackendCalibration] = {}
    pinned: set[str] = set()
    tuning_state = states.get(TUNING_FILE)
    if tuning_state is not None:
        pinned.update(f"tuning/{path}" for path in tuning_state.pins)
    for backend in KNOWN_BACKENDS:
        calibration, state = _load_backend(files, backend, written, issues)
        pinned.update(f"{backend}/{path}" for path in state.pins)
        backends[backend] = calibration
        states[state.name] = state
    return LoadedKnobs(tunables, Calibration(backends, frozenset(pinned)), tuple(issues), states, signature)


class KnobReader:
    """The loaded knobs, read again only when a knob file changed. Safe to call from any thread."""

    def __init__(self, files: KnobFiles) -> None:
        self.files = files
        self._lock = threading.Lock()
        self._loaded: LoadedKnobs | None = None

    def current(self) -> LoadedKnobs:
        with self._lock:
            signature = self.files.signature()
            if self._loaded is None or self._loaded.signature != signature:
                self._loaded = load_knobs(self.files)
            return self._loaded


# ------------------------------------------------------------------ the commands' operations


@dataclass(frozen=True)
class ShowRow:
    file: str
    path: str
    value: Any
    source: str
    approved_against: str | None = None


def show(files: KnobFiles) -> list[ShowRow]:
    """Every effective value with where it came from: factory, provisional, learned, pinned, backup, or the tunables file."""
    loaded = load_knobs(files)
    written = files.read_written()
    rows: list[ShowRow] = []
    tuning = tunables_to_file(loaded.tunables)
    factory_tuning = flatten(tunables_to_file(factory_tunables()))
    state = loaded.files.get(TUNING_FILE)
    for path, value in flatten(tuning).items():
        source = KNOBS_PINNED if state is not None and path in state.pins else "backup" if state is not None and state.origin == "backup" else "file" if value != factory_tuning.get(path) else "factory"
        rows.append(ShowRow(TUNING_FILE, path, value, source))
    for backend in KNOWN_BACKENDS:
        name = backend_file(backend)
        calibration = loaded.calibration.backends[backend]
        state = loaded.files.get(name)
        factory_flat = flatten(backend_to_mapping(factory_calibration().backends[backend]))
        for path, value in flatten(backend_to_mapping(calibration)).items():
            parts = path.split("/")
            learned = len(parts) > 1 and parts[0] == "pipelines" and parts[1] in calibration.pipelines and calibration.pipelines[parts[1]].learned
            if state is not None and path in state.pins:
                source = KNOBS_PINNED
            elif state is not None and state.origin == "backup":
                source = "backup"
            elif learned and value != factory_flat.get(path):
                source = KNOBS_LEARNED if calibration.pipelines[parts[1]].source == KNOBS_LEARNED else "provisional"
            else:
                source = KNOBS_FACTORY
            group = f"{backend}/{parts[1]}" if len(parts) > 1 and parts[0] == "pipelines" else ""
            record = written.groups.get(group, {})
            approved_against = record.get("approved_against") if source == KNOBS_LEARNED and isinstance(record, Mapping) and record.get("approved_against") in ("factory", "learned") else None
            rows.append(ShowRow(name, path, value, source, approved_against))
    return rows


@dataclass(frozen=True)
class CheckReport:
    issues: tuple[Issue, ...]
    suspended: tuple[str, ...]
    pins: Mapping[str, Mapping[str, Any]]
    deferred: tuple[str, ...]
    gates: Mapping[str, str]
    search: str | None
    files: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.issues and not self.suspended


def check(files: KnobFiles) -> CheckReport:
    loaded = load_knobs(files)
    _document, _baselines, validated_problem = read_validated(files)
    written = files.read_written()
    counts, entries = files.read_ring()
    deferred = []
    gates: dict[str, str] = {}
    for group, record in sorted(written.groups.items()):
        pending = counts.get(group, 0) - int(record.get("count", 0)) if isinstance(record, dict) else 0
        if pending > 0:
            deferred.append(f"{group}: {pending} run(s) in the ring are not yet processed")
        gate = record.get("last_gate") if isinstance(record, dict) else None
        if isinstance(gate, dict) and all(isinstance(gate.get(name), str) for name in ("candidate", "candidate_reason", "live", "live_reason")) and isinstance(gate.get("holdout_executions"), int) and not isinstance(gate.get("holdout_executions"), bool) and isinstance(gate.get("through_execution"), int) and not isinstance(gate.get("through_execution"), bool):
            gates[group] = f"candidate {gate['candidate']} ({gate['candidate_reason']}), live {gate['live']} ({gate['live_reason']}), {gate['holdout_executions']} held-out execution(s) through {gate['through_execution']}"
    for name in sorted(loaded.suspended):
        deferred.append(f"{name}: automatic updates are suspended")
    suspended = set(loaded.suspended)
    if validated_problem:
        suspended.add(VALIDATED_FILE)
        deferred.append(f"{VALIDATED_FILE}: automatic approvals are suspended")
    del entries
    issues = loaded.issues + ((Issue(VALIDATED_FILE, validated_problem, "error"),) if validated_problem else ())
    present = {name for name, state in loaded.files.items() if state.present}
    if files.path_of(VALIDATED_FILE).exists():
        present.add(VALIDATED_FILE)
    last_search = written.search.get("last_result")
    search = json.dumps(last_search, sort_keys=True) if isinstance(last_search, Mapping) else None
    return CheckReport(issues, tuple(sorted(suspended)), {name: dict(state.pins) for name, state in loaded.files.items() if state.pins}, tuple(deferred), gates, search, tuple(sorted(present)))


def factory_texts() -> dict[str, str]:
    """The factory knobs as the example files: ``tuning.example.yaml`` and one per backend."""
    texts = {f"tuning{EXAMPLE_SUFFIX}": dump_yaml(tunables_to_file(factory_tunables()))}
    for backend, calibration in factory_calibration().backends.items():
        texts[f"{backend}{EXAMPLE_SUFFIX}"] = dump_yaml(backend_to_mapping(calibration))
    return texts


def export_defaults(directory: Path) -> list[Path]:
    """Write the factory example files into ``directory`` (created if absent)."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in factory_texts().items():
        path = directory / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def reset(files: KnobFiles, backend: str | None = None) -> list[str]:
    """Move a backend's file (or every knob file) to the backups so the factory applies, and start its learning over from the
    ring. Returns the files moved."""
    names = [backend_file(backend)] if backend is not None else [backend_file(item) for item in KNOWN_BACKENDS]
    if backend is not None and backend not in KNOWN_BACKENDS:
        raise FramecontrolError(f"{backend} is not a backend with a timing knob file; known: {', '.join(KNOWN_BACKENDS)}")
    with files.lock():
        files.recover()
        keep = load_knobs(files).tunables.backup_count
        moved = [name for name in names if files.path_of(name).exists()]
        written = files.read_written()
        for name in moved:
            written.files.pop(name, None)
        for group in list(written.groups):
            if backend is None or group.startswith(f"{backend}/"):
                written.groups[group] = {"count": 0, "checkpoint": 0}
        # A reset makes the processed-run total move backwards.  Start the
        # global search cadence from that new total and discard the stale
        # result rather than waiting for the old pre-reset total to be passed.
        written.search = {"last_attempt_total": sum(int(record.get("count", 0)) for record in written.groups.values() if isinstance(record, Mapping))}
        # Validated baselines are machine state too: resetting all backends
        # removes it, while a targeted reset drops only that backend's entry.
        validated = files.read_text(VALIDATED_FILE)
        texts: dict[str, str | None] = dict.fromkeys(moved)
        if backend is None:
            texts[VALIDATED_FILE] = None
            written.files.pop(VALIDATED_FILE, None)
        elif validated is not None:
            _document, _baselines, problem = read_validated(files)
            if problem:
                raise FramecontrolError(f"{VALIDATED_FILE} cannot be used; reset all backends to remove it")
            try:
                document = json.loads(validated)
            except ValueError:
                raise FramecontrolError(f"{VALIDATED_FILE} is damaged; reset all backends to remove it") from None
            if not isinstance(document, dict) or document.get("schema") != 1 or not isinstance(document.get("backends"), dict):
                raise FramecontrolError(f"{VALIDATED_FILE} is damaged; reset all backends to remove it")
            if backend in document["backends"]:
                document["backends"].pop(backend)
                text = json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
                texts[VALIDATED_FILE] = text
                written.files[VALIDATED_FILE] = {"digest": digest(text), "values": {}, "pins": {}}
        files.commit(texts, written, keep=keep)
        files.log(f"moved {', '.join(moved) or 'nothing'} to the backups")
    return moved


def rollback(files: KnobFiles, count: int = 1) -> list[str]:
    with files.lock():
        files.recover()
        return files.rollback(count, load_knobs(files).tunables.backup_count)


def update_now(paths: ProjectPaths, *, force_search: bool = False) -> UpdateReport:
    """Run one update now, as the server's updater would, from the runs the state database holds (read only; a missing
    database is no runs). It takes the same lock, so it can run beside a server."""
    from framecontrol.services.knob_updater import KnobUpdateEngine, runs_of_store
    from framecontrol.state.store import Store, StoreMode

    runs = []
    if paths.database.exists():
        store = Store.open(paths.database, mode=StoreMode.BROWSE)
        try:
            runs = runs_of_store(store)
        finally:
            store.close()
    return KnobUpdateEngine(knob_files(paths)).update(runs, force_search=force_search)


def tune_now(paths: ProjectPaths) -> UpdateReport:
    """Run the same guarded background pass on demand.

    The M15 search shares the updater's lock and eligibility checks; exposing
    this entry point keeps the CLI from acquiring a second, racy lock.
    """
    return update_now(paths, force_search=True)
