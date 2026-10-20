"""The updater: it keeps the ring of complete runs, counts them per backend and pipeline, and at completed-run counts 1, 2, 4,
8, 16 and 32 writes a provisional calibration to the knob files. It runs one update at a time on a low-priority background
thread, only while the queue is idle or cooling down, and a run that finishes during an update is coalesced into the next.
Nothing here raises into the worker.

``KnobUpdateEngine.update`` is the whole step, synchronous and under the files' lock, so ``fc timing knobs update`` runs the
same code with no server. Later runs enter the ring and may pass the held-out gate."""

from __future__ import annotations

import ctypes
import json
import math
import os
import sys
import threading
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from loguru import logger

from framecontrol.core.backend_ids import KNOWN_BACKENDS
from framecontrol.core.paths import ProjectPaths
from framecontrol.jobs.timing.calibrate import CHECKPOINTS, calibrate
from framecontrol.jobs.timing.factory import factory_calibration, factory_tunables
from framecontrol.jobs.timing.features import HistoryRun, feature_values
from framecontrol.jobs.timing.fit import read_row
from framecontrol.jobs.timing.gate import decide, partition
from framecontrol.jobs.timing.knob_codec import backend_from_mapping, backend_to_mapping, flatten, pipeline_to_mapping, tunables_to_file
from framecontrol.jobs.timing.knobs import KNOBS_LEARNED, BackendCalibration, PipelineCalibration
from framecontrol.jobs.timing.ring import history_run as ring_history_run
from framecontrol.jobs.timing.ring import ring_entry, valid_entry
from framecontrol.jobs.timing.step_terms import learned_terms
from framecontrol.services.timing_history import history_run
from framecontrol.services.timing_knob_files import TUNING_FILE, VALIDATED_FILE, KnobBusyError, KnobConflictError, KnobFiles, Written, digest, dump_yaml, stamp_now
from framecontrol.services.timing_knobs import FileState, LoadedKnobs, backend_file, extract_readonly, load_knobs, pins_of_state, read_validated

if TYPE_CHECKING:
    from framecontrol.state.store import Store

FROZEN_AFTER = CHECKPOINTS[-1]
POLL_SECONDS = 1.0


@dataclass
class UpdateReport:
    runs_added: int = 0
    written: list[str] = field(default_factory=list)
    fitted: dict[str, int] = field(default_factory=dict)
    rejected: list[str] = field(default_factory=list)
    suspended: list[str] = field(default_factory=list)
    gates: dict[str, str] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.written or self.runs_added)


def runs_of_store(store: Store) -> list[HistoryRun]:
    """The store's complete live runs, as the fit reads them."""
    rows, _skipped = store.timing.complete_runs()
    return [history_run(row) for row in rows if row.fidelity == "live"]


def group_of(entry: Mapping[str, Any]) -> str:
    return f"{entry['b']}/{entry['p']}"


def _set_path(mapping: dict[str, Any], path: str, value: Any) -> None:
    node = mapping
    parts = path.split("/")
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = node[part] = {}
        node = child
    node[parts[-1]] = value


def _drop_path(mapping: dict[str, Any], path: str) -> None:
    node = mapping
    parts = path.split("/")
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            return
        node = child
    node.pop(parts[-1], None)


class KnobUpdateEngine:
    """One update of the knob files from the runs a store holds."""

    def __init__(self, files: KnobFiles, clock: Callable[[], str] = stamp_now) -> None:
        self._files = files
        self._clock = clock

    def update(self, runs: Sequence[HistoryRun]) -> UpdateReport:
        report = UpdateReport()
        try:
            with self._files.lock():
                self._files.recover()
                self._update(runs, report)
        except KnobBusyError as error:
            report.rejected.append(str(error))
        except KnobConflictError as error:
            self._files.log(str(error))
            report.rejected.append(str(error))
        except Exception:
            logger.exception("The timing knob update failed; the files are as they were")
            self._files.log("update failed with an unexpected error; see the server log")
            report.rejected.append("the update failed unexpectedly")
        return report

    def _update(self, runs: Sequence[HistoryRun], report: UpdateReport) -> None:
        files = self._files
        loaded = load_knobs(files)
        tunables = loaded.tunables
        validated_digest = files.current_digest(VALIDATED_FILE)
        validated_document, validated_baselines, validated_problem = read_validated(files)
        written = files.read_written()
        before = json.dumps(written.as_dict(), sort_keys=True)
        for name, state in loaded.files.items():
            record = written.files.get(name)
            if isinstance(record, dict) and state.present and not state.suspended:
                record["pins"] = dict(state.pins)
        ring_state = files.read_ring_state()
        counts, high = ring_state.added, dict(ring_state.high)
        entries = [entry for entry in ring_state.entries if valid_entry(entry)]
        for entry in entries:
            # A ring written before the marks existed: its newest run per group is the mark.
            high[group_of(entry)] = max(high.get(group_of(entry), (0, 0)), (entry["e"], entry["r"]))
        keys = {(entry["b"], entry["e"], entry["r"]) for entry in entries}
        for run in sorted(runs, key=lambda item: (item.execution, item.run)):
            entry = ring_entry(run) if run.backend in KNOWN_BACKENDS else None
            if entry is None or (entry["b"], entry["e"], entry["r"]) in keys:
                continue
            group = group_of(entry)
            if (entry["e"], entry["r"]) <= high.get(group, (-1, -1)):
                # Added before and since trimmed from the ring (or older than the newest it has seen): not a new run.
                continue
            keys.add((entry["b"], entry["e"], entry["r"]))
            entries.append(entry)
            high[group] = (entry["e"], entry["r"])
            counts[group] = counts.get(group, 0) + 1
            report.runs_added += 1
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for entry in entries:
            grouped[group_of(entry)].append(entry)
        for group, members in grouped.items():
            counts.setdefault(group, len(members))
            if len(members) > tunables.ring_size:
                dropped = {id(entry) for entry in members[: len(members) - tunables.ring_size]}
                entries = [entry for entry in entries if id(entry) not in dropped]
                grouped[group] = members[len(members) - tunables.ring_size :]
        replacements: dict[str, dict[str, PipelineCalibration]] = defaultdict(dict)
        approved: dict[str, PipelineCalibration] = {}
        for group in sorted(grouped):
            backend, _, pipeline = group.partition("/")
            state = loaded.files.get(backend_file(backend))
            record = written.groups.setdefault(group, {"count": 0, "checkpoint": 0})
            processed, added = int(record.get("count", 0)), counts[group]
            if added <= processed:
                continue
            if state is not None and state.suspended:
                if backend_file(backend) not in report.suspended:
                    report.suspended.append(backend_file(backend))
                continue
            record["count"] = added
            if added > FROZEN_AFTER and record.get("checkpoint", 0) >= FROZEN_AFTER and not isinstance(record.get("checkpoint_execution"), int):
                # Older written records do not have this mark. Reconstruct it
                # only when the whole prefix remains in the ring; otherwise
                # start after the newest known execution, conservatively.
                complete_prefix = added == len(grouped[group]) and len(grouped[group]) >= FROZEN_AFTER
                record["checkpoint_execution"] = grouped[group][FROZEN_AFTER - 1]["e"] if complete_prefix else max(item["e"] for item in grouped[group])
            crossed = [point for point in CHECKPOINTS if processed < point <= added]
            if not crossed:
                if added > FROZEN_AFTER:
                    if validated_problem:
                        report.gates[group] = "validated_baseline_unavailable"
                    else:
                        fitted = self._validated_fit(loaded, backend, pipeline, grouped[group], record, state, report, validated_baselines, approved)
                        if fitted is not None:
                            replacements[backend][pipeline] = fitted
                continue
            checkpoint = max(crossed)
            fitted = self._fit(loaded, backend, pipeline, grouped[group], added, checkpoint, state, report)
            if fitted is not None:
                replacements[backend][pipeline] = fitted
                record["checkpoint"] = checkpoint
                record["fit_at"] = fitted.updated_at
                report.fitted[group] = checkpoint
                if checkpoint == FROZEN_AFTER:
                    complete_prefix = added == len(grouped[group]) and len(grouped[group]) >= FROZEN_AFTER
                    record["checkpoint_execution"] = grouped[group][FROZEN_AFTER - 1]["e"] if complete_prefix else max(item["e"] for item in grouped[group])
        texts = self._texts(loaded, replacements, written, report)
        if approved:
            approval_tunables = tunables_to_file(loaded.tunables)
            tuning_state = loaded.files.get(TUNING_FILE)
            tuning_record = written.files.get(TUNING_FILE)
            machine_tunables = tuning_record.get("values", {}) if isinstance(tuning_record, Mapping) else {}
            factory_tuning = flatten(tunables_to_file(factory_tunables()))
            for path in tuning_state.pins if tuning_state is not None else ():
                _set_path(approval_tunables, path, machine_tunables.get(path, factory_tuning[path]))
            for group, calibration in approved.items():
                backend, _, pipeline = group.partition("/")
                bucket = validated_document["backends"].setdefault(backend, {})
                bucket[pipeline] = {"approved_at": calibration.updated_at, "runs": calibration.runs, "executions": calibration.executions, "holdout_executions": written.groups[group].get("last_gate_holdout_executions", 0), "calibration": pipeline_to_mapping(calibration), "tunables": approval_tunables}
            text = json.dumps(validated_document, sort_keys=True, separators=(",", ":")) + "\n"
            texts[VALIDATED_FILE] = text
            written.files[VALIDATED_FILE] = {"digest": digest(text), "values": {}, "pins": {}}
        ring = files.ring_text(counts, entries, high)
        if not texts and files.read_text("ring.jsonl") == ring and json.dumps(written.as_dict(), sort_keys=True) == before:
            return
        expected = {name: loaded.files[name].digest for name in texts if name in loaded.files}
        if VALIDATED_FILE in texts:
            expected[VALIDATED_FILE] = validated_digest
        files.commit(texts, written, ring=ring, keep=tunables.backup_count, expected=expected)
        report.written = sorted(texts)
        for name in report.written:
            files.log(f"wrote {name}")

    def _validated_fit(self, loaded: LoadedKnobs, backend: str, pipeline: str, members: list[dict[str, Any]], record: dict[str, Any], state: FileState | None, report: UpdateReport, baselines: Mapping[str, PipelineCalibration], approved: dict[str, PipelineCalibration]) -> PipelineCalibration | None:
        """Try one post-checkpoint fit only after three fresh executions.

        The ring is the sole input.  In particular, the held-out members are
        removed before fitting, even if the state database still retains them.
        """
        checkpoint_execution = record.get("checkpoint_execution")
        split = partition(members, checkpoint_execution if isinstance(checkpoint_execution, int) else None)
        group = f"{backend}/{pipeline}"
        if split.reason:
            report.gates[group] = split.reason
            return None
        held_ids = sorted({int(item["e"]) for item in split.holdout})
        previous = int(record.get("last_gate_execution", -1))
        if previous >= 0 and sum(item > previous for item in held_ids) < 3:
            return None
        pins = pins_of_state(state, pipeline) if state is not None else {}
        numeric = {path: float(value) for path, value in pins.items() if isinstance(value, int | float) and not isinstance(value, bool)}
        factory = factory_calibration().backends[backend].baseline_pipeline(pipeline)
        baseline = baselines.get(group, factory)
        candidate = calibrate(baseline, baseline, [ring_history_run(entry) for entry in split.training], loaded.tunables, checkpoint=int(record.get("checkpoint", FROZEN_AFTER)), stamp=self._clock(), pins=numeric, source=KNOBS_LEARNED)
        candidate_was_fit = candidate is not None
        candidate_invalid = False

        def pinned(calibration: PipelineCalibration, label: str) -> PipelineCalibration | None:
            mapping = pipeline_to_mapping(calibration)
            for path, value in pins.items():
                _set_path(mapping, path, value)
            checked, problems = backend_from_mapping({"pipelines": {pipeline: mapping}}, factory_calibration().backends[backend])
            if problems:
                report.rejected.append(f"{group}: invalid {label} calibration ({'; '.join(problems[:3])})")
                return None
            return checked.pipelines[pipeline]

        if candidate is not None:
            candidate = pinned(candidate, "candidate")
            candidate_invalid = candidate is None
        pinned_baseline = pinned(baseline, "validated baseline")
        live = pinned(loaded.calibration.backends[backend].pipelines.get(pipeline, factory), "live")
        if pinned_baseline is None or live is None:
            report.gates[group] = "validated_baseline_unavailable"
            return None

        def log_rate(calibration: PipelineCalibration, run: HistoryRun) -> float | None:
            pixels, frames = run.workload.get("pixels"), run.workload.get("frames")
            if not isinstance(pixels, int | float) or not isinstance(frames, int | float) or isinstance(pixels, bool) or isinstance(frames, bool):
                return None
            try:
                terms = learned_terms(calibration.step, run.workload, feature_values(run.workload))
                rate = calibration.step.rate(float(pixels), float(frames), terms)
                return math.log(rate) if math.isfinite(rate) and rate > 0 else None
            except (ValueError, OverflowError, ZeroDivisionError):
                return None

        candidate_differences: list[float] = []
        live_differences: list[float] = []
        candidate_ids: list[int] = []
        live_ids: list[int] = []
        live_invalid = False
        for execution in held_ids:
            runs = [ring_history_run(entry) for entry in split.holdout if int(entry["e"]) == execution]
            candidate_errors: list[float] = []
            live_errors: list[float] = []
            for run in runs:
                row = read_row(run, loaded.tunables)
                if row.y is not None:
                    old = log_rate(pinned_baseline, run)
                    if old is None:
                        continue
                    current = log_rate(live, run)
                    if current is None:
                        live_invalid = True
                    else:
                        live_errors.append(abs(current - row.y) - abs(old - row.y))
                    if candidate is not None and not candidate_invalid:
                        new = log_rate(candidate, run)
                        if new is None:
                            candidate_invalid = True
                            candidate_errors.clear()
                        else:
                            candidate_errors.append(abs(new - row.y) - abs(old - row.y))
            if candidate_errors:
                candidate_differences.append(sum(candidate_errors) / len(candidate_errors))
                candidate_ids.append(execution)
            if live_errors:
                live_differences.append(sum(live_errors) / len(live_errors))
                live_ids.append(execution)
        if candidate_invalid:
            result = decide(candidate_differences, candidate_ids, valid=False)
        elif not candidate_was_fit:
            result = decide((), ())
        else:
            result = decide(candidate_differences, candidate_ids)
        live_result = decide(live_differences, live_ids, valid=not live_invalid)
        record["last_gate_execution"] = max(held_ids)
        record["last_gate_holdout_executions"] = len(candidate_ids)
        record["last_gate"] = {"candidate": result.verdict, "candidate_reason": result.reason, "live": live_result.verdict, "live_reason": live_result.reason, "holdout_executions": len(candidate_ids), "through_execution": max(held_ids)}
        report.gates[group] = result.verdict
        self._files.log(f"{group}: candidate {result.verdict} ({result.reason}; holdout={len(candidate_ids)}; interval={result.interval}); live {live_result.verdict} ({live_result.reason}; holdout={len(live_ids)}; interval={live_result.interval})")
        if result.verdict == "promote":
            assert candidate is not None
            approval_mapping = pipeline_to_mapping(candidate)
            baseline_mapping = pipeline_to_mapping(baseline)
            baseline_values = flatten(baseline_mapping)
            for path in pins:
                if path in baseline_values:
                    _set_path(approval_mapping, path, baseline_values[path])
                else:
                    _drop_path(approval_mapping, path)
            checked, problems = backend_from_mapping({"pipelines": {pipeline: approval_mapping}}, factory_calibration().backends[backend])
            if problems:
                report.gates[group] = "invalid_candidate"
                record["last_gate"]["candidate"] = "reject"
                record["last_gate"]["candidate_reason"] = "invalid_approval_without_pins"
                report.rejected.append(f"{group}: invalid approval without pins ({'; '.join(problems[:3])})")
                return None
            approved[group] = checked.pipelines[pipeline]
            return candidate
        if live_result.verdict == "reject":
            report.gates[group] = "restored"
            return pinned_baseline
        return None

    def _fit(self, loaded: LoadedKnobs, backend: str, pipeline: str, members: list[dict[str, Any]], added: int, checkpoint: int, state: FileState | None, report: UpdateReport) -> PipelineCalibration | None:
        factory = factory_calibration().backends[backend]
        current = loaded.calibration.backends[backend]
        dropped = added - len(members)
        used = members[: max(1, checkpoint - dropped)]
        history = [ring_history_run(entry) for entry in used]
        pins = pins_of_state(state, pipeline) if state is not None else {}
        numeric = {path: float(value) for path, value in pins.items() if isinstance(value, int | float) and not isinstance(value, bool)}
        candidate = calibrate(factory.baseline_pipeline(pipeline), current.pipelines.get(pipeline), history, loaded.tunables, checkpoint=checkpoint, stamp=self._clock(), pins=numeric)
        group = f"{backend}/{pipeline}"
        if candidate is None:
            self._files.log(f"{group}: checkpoint {checkpoint} had no step times to learn from; nothing was written")
            return None
        mapping = pipeline_to_mapping(candidate)
        for path, value in pins.items():
            _set_path(mapping, path, value)
        checked, problems = backend_from_mapping({"pipelines": {pipeline: mapping}}, factory)
        if problems:
            reason = f"{group}: the candidate at checkpoint {checkpoint} was rejected ({'; '.join(problems[:3])}); the files are unchanged"
            self._files.log(reason)
            report.rejected.append(reason)
            return None
        return checked.pipelines[pipeline]

    def _texts(self, loaded: LoadedKnobs, replacements: Mapping[str, Mapping[str, PipelineCalibration]], written: Written, report: UpdateReport) -> dict[str, str]:
        texts: dict[str, str] = {}
        stamp = self._clock()
        tuning = loaded.files.get(TUNING_FILE)
        if tuning is not None and not tuning.present and not tuning.suspended:
            text = dump_yaml(tunables_to_file(loaded.tunables))
            texts[TUNING_FILE] = text
            written.files[TUNING_FILE] = {"digest": digest(text), "values": flatten(tunables_to_file(loaded.tunables)), "pins": {}}
        for backend in KNOWN_BACKENDS:
            name = backend_file(backend)
            state = loaded.files.get(name)
            calibration: BackendCalibration = loaded.calibration.backends[backend]
            if state is not None and state.suspended:
                if name not in report.suspended:
                    report.suspended.append(name)
                continue
            missing = state is None or not state.present
            if not replacements.get(backend) and not missing:
                continue
            if replacements.get(backend):
                calibration = replace(calibration, pipelines={**calibration.pipelines, **replacements[backend]}, updated_at=stamp)
            mapping = backend_to_mapping(calibration)
            text = dump_yaml(mapping)
            if state is not None and state.present and self._files.read_text(name) == text:
                continue
            texts[name] = text
            pins = dict(state.pins) if state is not None else {}
            written.files[name] = {"digest": digest(text), "values": flatten(mapping), "pins": pins, "readonly": extract_readonly(mapping)}
        return texts


# ------------------------------------------------------------------ the background thread


def lower_thread_priority() -> None:
    """Ask the system to run this thread at background priority; where it cannot, the thread runs as it is."""
    try:
        if sys.platform == "darwin":
            ctypes.CDLL(None).pthread_set_qos_class_self_np(0x09, 0)
        elif hasattr(os, "setpriority") and hasattr(os, "PRIO_PROCESS"):
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
    except Exception:
        logger.debug("The knob updater could not lower its thread's priority")


class KnobUpdater:
    """Runs updates off the worker's thread. ``notify`` after a run completes is all the worker does; the update waits until
    ``quiet`` says no run is generating, and notifications that arrive meanwhile are one update."""

    def __init__(self, paths: ProjectPaths, store: Store, quiet: Callable[[], bool] = lambda: True, *, poll: float = POLL_SECONDS, engine: KnobUpdateEngine | None = None) -> None:
        self._store = store
        self._quiet = quiet
        self._poll = poll
        self._engine = engine or KnobUpdateEngine(KnobFiles(paths.timing_data))
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._thread: threading.Thread | None = None
        self.last_report: UpdateReport | None = None

    def set_quiet(self, quiet: Callable[[], bool]) -> None:
        """Say when the queue is idle or cooling down (the worker, once built, is what knows)."""
        self._quiet = quiet

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="knob-updater", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
            self._thread = None

    def notify(self) -> None:
        """A run completed: schedule an update. Cheap and never raises."""
        self._idle.clear()
        self._wake.set()

    def wait_idle(self, timeout: float) -> bool:
        """Whether no update is pending or running within ``timeout`` seconds (for tests and ``fc`` shutdown)."""
        return self._idle.wait(timeout)

    def _run(self) -> None:
        lower_thread_priority()
        while not self._stop.is_set():
            self._wake.wait()
            if self._stop.is_set():
                return
            self._wake.clear()
            while not self._stop.is_set() and not self._safe_quiet():
                self._stop.wait(self._poll)
            if self._stop.is_set():
                return
            try:
                self.last_report = self._engine.update(runs_of_store(self._store))
            except Exception:
                logger.exception("The timing knob update could not read the store")
            if not self._wake.is_set():
                self._idle.set()

    def _safe_quiet(self) -> bool:
        try:
            return self._quiet()
        except Exception:
            return False
