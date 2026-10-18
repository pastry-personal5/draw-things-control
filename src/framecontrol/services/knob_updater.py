"""The updater: it keeps the ring of complete runs, counts them per backend and pipeline, and at completed-run counts 1, 2, 4,
8, 16 and 32 writes a provisional calibration to the knob files. It runs one update at a time on a low-priority background
thread, only while the queue is idle or cooling down, and a run that finishes during an update is coalesced into the next.
Nothing here raises into the worker.

``KnobUpdateEngine.update`` is the whole step, synchronous and under the files' lock, so ``fc timing knobs update`` runs the
same code with no server. After the 32nd run of a pipeline its learning is frozen; every later run still enters the ring."""

from __future__ import annotations

import ctypes
import json
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
from framecontrol.jobs.timing.factory import factory_calibration
from framecontrol.jobs.timing.features import HistoryRun
from framecontrol.jobs.timing.knob_codec import backend_from_mapping, backend_to_mapping, flatten, pipeline_to_mapping, tunables_to_file
from framecontrol.jobs.timing.knobs import BackendCalibration, PipelineCalibration
from framecontrol.jobs.timing.ring import history_run as ring_history_run
from framecontrol.jobs.timing.ring import ring_entry, valid_entry
from framecontrol.services.timing_history import history_run
from framecontrol.services.timing_knob_files import TUNING_FILE, KnobBusyError, KnobConflictError, KnobFiles, Written, digest, dump_yaml, stamp_now
from framecontrol.services.timing_knobs import FileState, LoadedKnobs, backend_file, extract_readonly, load_knobs, pins_of_state

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
        written = files.read_written()
        before = json.dumps(written.as_dict(), sort_keys=True)
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
            crossed = [point for point in CHECKPOINTS if processed < point <= added]
            if not crossed:
                continue
            checkpoint = max(crossed)
            fitted = self._fit(loaded, backend, pipeline, grouped[group], added, checkpoint, state, report)
            if fitted is not None:
                replacements[backend][pipeline] = fitted
                record["checkpoint"] = checkpoint
                record["fit_at"] = fitted.updated_at
                report.fitted[group] = checkpoint
        texts = self._texts(loaded, replacements, written, report)
        ring = files.ring_text(counts, entries, high)
        if not texts and files.read_text("ring.jsonl") == ring and json.dumps(written.as_dict(), sort_keys=True) == before:
            return
        expected = {name: loaded.files[name].digest for name in texts if name in loaded.files}
        files.commit(texts, written, ring=ring, keep=tunables.backup_count, expected=expected)
        report.written = sorted(texts)
        for name in report.written:
            files.log(f"wrote {name}")

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
