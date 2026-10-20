"""The updater: it keeps the ring of complete runs, counts them per backend and pipeline, and at completed-run counts 1, 2, 4,
8, 16 and 32 writes a provisional calibration to the knob files. It runs one update at a time on a low-priority background
thread, only while the queue is idle or cooling down, and a run that finishes during an update is coalesced into the next.
Nothing here raises into the worker.

``KnobUpdateEngine.update`` is the whole step, synchronous and under the files' lock, so ``fc timing knobs update`` runs the
same code with no server. Later runs enter the ring and may pass the held-out gate."""

from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from loguru import logger

from framecontrol.core.backend_ids import KNOWN_BACKENDS
from framecontrol.core.paths import ProjectPaths
from framecontrol.jobs.timing.calibrate import CHECKPOINTS, calibrate, calibrate_validated
from framecontrol.jobs.timing.factory import factory_calibration, factory_tunables
from framecontrol.jobs.timing.features import HistoryRun
from framecontrol.jobs.timing.gate import compare_replays, coverage_regressed, decide, partition
from framecontrol.jobs.timing.knob_codec import backend_from_mapping, backend_to_mapping, flatten, pipeline_to_mapping, tunables_from_file, tunables_to_file
from framecontrol.jobs.timing.knobs import KNOBS_LEARNED, BackendCalibration, Calibration, PipelineCalibration, Tunables
from framecontrol.jobs.timing.ring import history_run as ring_history_run
from framecontrol.jobs.timing.ring import ring_entry, valid_entry
from framecontrol.jobs.timing.tune import SEARCH_CONFIDENCE, SearchGroup, select
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
    search: str | None = None

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

    def update(self, runs: Sequence[HistoryRun], *, force_search: bool = False, should_stop: Callable[[], bool] = lambda: False) -> UpdateReport:
        report = UpdateReport()
        try:
            with self._files.lock():
                self._files.recover()
                self._update(runs, report, force_search=force_search, should_stop=should_stop)
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

    def _update(self, runs: Sequence[HistoryRun], report: UpdateReport, *, force_search: bool, should_stop: Callable[[], bool]) -> None:
        files = self._files
        loaded = load_knobs(files)
        tunables = loaded.tunables
        validated_digest = files.current_digest(VALIDATED_FILE)
        validated_document, validated_baselines, validated_problem = read_validated(files)
        validated_tunables: dict[str, Tunables] = {}
        for backend, pipelines in validated_document.get("backends", {}).items():
            if isinstance(pipelines, Mapping):
                for pipeline, record in pipelines.items():
                    if isinstance(record, Mapping) and isinstance(record.get("tunables"), Mapping):
                        parsed, problems = tunables_from_file(record["tunables"])
                        if not problems:
                            validated_tunables[f"{backend}/{pipeline}"] = parsed
        written = files.read_written()
        tuning_record_before = written.files.get(TUNING_FILE)
        machine_tuning_before = tuning_record_before.get("values", {}) if isinstance(tuning_record_before, Mapping) else {}
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
        approval_tunables: dict[str, Tunables] = {}
        restored_tunables: list[Tunables] = []
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
                        fitted = self._validated_fit(loaded, backend, pipeline, grouped[group], runs, record, state, report, validated_baselines, validated_tunables, approved, restored_tunables)
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
        searched = restored_tunables[-1] if restored_tunables else None
        if not approved and not restored_tunables and not validated_problem:
            searched = self._search(loaded, grouped, written, report, validated_baselines, replacements, approved, approval_tunables, force=force_search, should_stop=should_stop)
        elif force_search and approved:
            report.search = "deferred_after_gate"
        texts = self._texts(loaded, replacements, written, report)
        if searched is not None:
            tuning_mapping = tunables_to_file(searched)
            state = loaded.files.get(TUNING_FILE)
            effective = flatten(tunables_to_file(loaded.tunables))
            for path in state.pins if state is not None else ():
                if path in effective:
                    _set_path(tuning_mapping, path, effective[path])
            text = dump_yaml(tuning_mapping)
            texts[TUNING_FILE] = text
            written.files[TUNING_FILE] = {"digest": digest(text), "values": flatten(tuning_mapping), "pins": dict(state.pins) if state is not None else {}}
        if approved:
            tuning_state = loaded.files.get(TUNING_FILE)
            factory_tuning = flatten(tunables_to_file(factory_tunables()))
            for group, calibration in approved.items():
                backend, _, pipeline = group.partition("/")
                bucket = validated_document["backends"].setdefault(backend, {})
                approved_tuning = tunables_to_file(approval_tunables.get(group, loaded.tunables))
                for path in tuning_state.pins if tuning_state is not None else ():
                    _set_path(approved_tuning, path, machine_tuning_before.get(path, factory_tuning[path]))
                bucket[pipeline] = {"approved_at": calibration.updated_at, "runs": calibration.runs, "executions": calibration.executions, "holdout_executions": written.groups[group].get("last_gate_holdout_executions", 0), "calibration": pipeline_to_mapping(calibration), "tunables": approved_tuning}
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

    @staticmethod
    def _with_pipeline(calibration: Calibration, backend: str, pipeline: str, value: PipelineCalibration) -> Calibration:
        current = calibration.backends[backend]
        changed = replace(current, pipelines={**current.pipelines, pipeline: value})
        return replace(calibration, backends={**calibration.backends, backend: changed})

    @staticmethod
    def _checked_pins(backend: str, pipeline: str, calibration: PipelineCalibration, pins: Mapping[str, Any]) -> PipelineCalibration | None:
        mapping = pipeline_to_mapping(calibration)
        for path, value in pins.items():
            _set_path(mapping, path, value)
        checked, problems = backend_from_mapping({"pipelines": {pipeline: mapping}}, factory_calibration().backends[backend])
        return None if problems else checked.pipelines[pipeline]

    @staticmethod
    def _approval_without_pins(backend: str, pipeline: str, candidate: PipelineCalibration, baseline: PipelineCalibration, pins: Mapping[str, Any]) -> PipelineCalibration | None:
        mapping = pipeline_to_mapping(candidate)
        baseline_values = flatten(pipeline_to_mapping(baseline))
        for path in pins:
            if path in baseline_values:
                _set_path(mapping, path, baseline_values[path])
            else:
                _drop_path(mapping, path)
        checked, problems = backend_from_mapping({"pipelines": {pipeline: mapping}}, factory_calibration().backends[backend])
        return None if problems else checked.pipelines[pipeline]

    def _search(self, loaded: LoadedKnobs, grouped: Mapping[str, list[dict[str, Any]]], written: Written, report: UpdateReport, baselines: Mapping[str, PipelineCalibration], replacements: dict[str, dict[str, PipelineCalibration]], approved: dict[str, PipelineCalibration], approval_tunables: dict[str, Tunables], *, force: bool, should_stop: Callable[[], bool]) -> Tunables | None:
        """Run one guarded cross-group tunables search, opening holdout only after selection."""
        total = sum(int(record.get("count", 0)) for record in written.groups.values() if isinstance(record, Mapping))
        previous = written.search.get("last_attempt_total", 0)
        previous = int(previous) if isinstance(previous, int) and not isinstance(previous, bool) else 0
        if not force and total - previous < 20:
            return None
        tuning_state = loaded.files.get(TUNING_FILE)
        if tuning_state is not None and tuning_state.suspended:
            report.search = "tuning_suspended"
            return None
        groups: list[SearchGroup] = []
        holdouts: dict[str, tuple[HistoryRun, ...]] = {}
        unpinned_baselines: dict[str, PipelineCalibration] = {}
        pin_sets: dict[str, Mapping[str, Any]] = {}
        for group, members in sorted(grouped.items()):
            executions = sorted({int(item["e"]) for item in members})
            if len(members) < 40 or len(executions) < 6:
                continue
            hold_count = min(8, max(3, (len(executions) + 4) // 5))
            hold_ids = frozenset(executions[-hold_count:])
            training = tuple(ring_history_run(item) for item in members if int(item["e"]) not in hold_ids)
            holdout = tuple(ring_history_run(item) for item in members if int(item["e"]) in hold_ids)
            if len({run.execution for run in training}) < 3 or len({run.execution for run in holdout}) < 3:
                continue
            backend, _, pipeline = group.partition("/")
            state = loaded.files.get(backend_file(backend))
            if state is not None and state.suspended:
                continue
            pins = pins_of_state(state, pipeline) if state is not None else {}
            numeric = {path: float(value) for path, value in pins.items() if isinstance(value, int | float) and not isinstance(value, bool)}
            baseline = baselines.get(group, factory_calibration().backends[backend].baseline_pipeline(pipeline))
            pinned = self._checked_pins(backend, pipeline, baseline, pins)
            if pinned is None:
                continue
            groups.append(SearchGroup(backend, pipeline, training, pinned, numeric))
            holdouts[group] = holdout
            unpinned_baselines[group] = baseline
            pin_sets[group] = pins
        if not groups:
            if force:
                report.search = "insufficient_evidence"
            return None
        written.search["last_attempt_total"] = total
        written.search["attempted_at"] = self._clock()
        if should_stop():
            report.search = "stopped"
            written.search["last_result"] = {"verdict": "stopped", "reason": "generation_started"}
            return None
        pinned_tunables = {path.split("/", 1)[1] for path in (tuning_state.pins if tuning_state is not None else ()) if path.startswith("tunables/")}
        rho_measured = any(loaded.calibration.backends[group.backend].pipeline(group.pipeline).executions >= 3 for group in groups)
        started = time.process_time()
        choice = select(loaded.tunables, loaded.calibration, groups, pinned=pinned_tunables, rho_measured=rho_measured, stamp=self._clock(), should_stop=should_stop, cpu_seconds=10.0)
        if choice is None:
            verdict = "stopped" if should_stop() else "timeout" if time.process_time() - started >= 10.0 else "no_improvement"
            report.search = verdict
            written.search["last_result"] = {"verdict": verdict, "reason": "generation_started" if verdict == "stopped" else "cpu_budget" if verdict == "timeout" else "training_selection"}
            self._files.log(f"tunables search {verdict} after {time.process_time() - started:.3f} CPU seconds")
            return None
        saved_candidates: dict[str, PipelineCalibration] = {}
        for group in groups:
            name = f"{group.backend}/{group.pipeline}"
            saved = self._approval_without_pins(group.backend, group.pipeline, choice.calibrations[name], unpinned_baselines[name], pin_sets[name])
            if saved is None:
                report.search = "invalid_candidate"
                written.search["last_result"] = {"verdict": "reject", "reason": f"{name}_invalid_without_pins"}
                self._files.log(f"tunables search rejected ({name} has an invalid calibration)")
                return None
            saved_candidates[name] = saved
        current_calibration = loaded.calibration
        candidate_calibration = loaded.calibration
        for group in groups:
            current_fit = calibrate_validated(group.baseline, group.training, loaded.tunables, checkpoint=FROZEN_AFTER, stamp=self._clock(), pins=group.pins, source=KNOBS_LEARNED)
            candidate_fit = choice.calibrations[f"{group.backend}/{group.pipeline}"]
            if current_fit is None:
                report.search = "invalid_candidate"
                written.search["last_result"] = {"verdict": "reject", "reason": "current_refit_failed"}
                return None
            current_calibration = self._with_pipeline(current_calibration, group.backend, group.pipeline, current_fit)
            candidate_calibration = self._with_pipeline(candidate_calibration, group.backend, group.pipeline, candidate_fit)
        held = [run for group in groups for run in holdouts[f"{group.backend}/{group.pipeline}"]]
        comparison = compare_replays(held, loaded.tunables, current_calibration, choice.tunables, candidate_calibration)
        result = decide(comparison.error_differences, comparison.execution_ids, valid=comparison.valid, confidence=SEARCH_CONFIDENCE)
        reason = result.reason
        clear_gain = result.verdict == "promote" and result.interval is not None and result.interval[1] < 0 and len(comparison.execution_ids) >= 5
        if clear_gain and coverage_regressed(comparison):
            clear_gain, reason = False, "coverage_regression"
        if clear_gain:
            for group in groups:
                name = f"{group.backend}/{group.pipeline}"
                local = compare_replays(holdouts[name], loaded.tunables, current_calibration, choice.tunables, candidate_calibration)
                local_result = decide(local.error_differences, local.execution_ids, valid=local.valid)
                if local_result.verdict == "reject" or coverage_regressed(local):
                    clear_gain, reason = False, f"{name}_regression"
                    break
        if not clear_gain or should_stop() or time.process_time() - started > 10.0:
            verdict = "stopped" if should_stop() else "timeout" if time.process_time() - started > 10.0 else "inconclusive"
            report.search = verdict
            written.search["last_result"] = {"verdict": verdict, "reason": reason, "tunable": choice.name, "holdout_executions": len(comparison.execution_ids), "confidence": SEARCH_CONFIDENCE, "error_interval": list(result.interval) if result.interval is not None else None}
            self._files.log(f"tunables search {verdict} ({reason}; {choice.name}; training={choice.current_error:.6f}->{choice.candidate_error:.6f}; holdout={len(comparison.execution_ids)}; confidence={SEARCH_CONFIDENCE:.0%}; interval={result.interval}; coverage_difference={comparison.coverage_differences}; CPU={time.process_time() - started:.3f}s)")
            return None
        for group in groups:
            name = f"{group.backend}/{group.pipeline}"
            candidate = choice.calibrations[name]
            replacements[group.backend][group.pipeline] = candidate
            approved[name] = saved_candidates[name]
            approval_tunables[name] = choice.tunables
            written.groups[name]["last_gate_holdout_executions"] = len({run.execution for run in holdouts[name]})
            written.groups[name]["approved_against"] = "learned" if name in baselines else "factory"
        report.search = f"promote {choice.name}"
        written.search["last_result"] = {"verdict": "promote", "reason": reason, "tunable": choice.name, "holdout_executions": len(comparison.execution_ids), "confidence": SEARCH_CONFIDENCE, "error_interval": list(result.interval) if result.interval is not None else None, "training_error": [choice.current_error, choice.candidate_error]}
        self._files.log(f"tunables search promoted {choice.name} ({choice.current_error:.6f}->{choice.candidate_error:.6f}; holdout={len(comparison.execution_ids)}; confidence={SEARCH_CONFIDENCE:.0%}; interval={result.interval}; CPU={time.process_time() - started:.3f}s)")
        return choice.tunables

    def _validated_fit(self, loaded: LoadedKnobs, backend: str, pipeline: str, members: list[dict[str, Any]], retained: Sequence[HistoryRun], record: dict[str, Any], state: FileState | None, report: UpdateReport, baselines: Mapping[str, PipelineCalibration], approved_tunables: Mapping[str, Tunables], approved: dict[str, PipelineCalibration], restored_tunables: list[Tunables]) -> PipelineCalibration | None:
        """Try one post-checkpoint fit only after three fresh executions.

        The ring and retained live history are deduplicated for training.  In
        particular, the held-out executions are removed from both sources.
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
        baseline_tunables = approved_tunables.get(group, factory_tunables())
        tuning_state = loaded.files.get(TUNING_FILE)
        tuning_pins = {path.split("/", 1)[1]: getattr(loaded.tunables, path.split("/", 1)[1]) for path in (tuning_state.pins if tuning_state is not None else ()) if "/" in path and hasattr(loaded.tunables, path.split("/", 1)[1])}
        baseline_tunables = replace(baseline_tunables, **tuning_pins)
        held_ids_set = {int(item["e"]) for item in split.holdout}
        training_by_key = {(run.execution, run.run): run for run in (ring_history_run(entry) for entry in split.training)}
        for run in retained:
            if run.fidelity == "live" and run.backend == backend and run.pipeline == pipeline and run.execution not in held_ids_set:
                training_by_key.setdefault((run.execution, run.run), run)
        training = list(training_by_key.values())
        candidate = calibrate_validated(baseline, training, loaded.tunables, checkpoint=int(record.get("checkpoint", FROZEN_AFTER)), stamp=self._clock(), pins=numeric, source=KNOBS_LEARNED)
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

        holdout = [ring_history_run(entry) for entry in split.holdout]
        baseline_model = self._with_pipeline(loaded.calibration, backend, pipeline, pinned_baseline)
        live_model = self._with_pipeline(loaded.calibration, backend, pipeline, live)
        live_comparison = compare_replays(holdout, baseline_tunables, baseline_model, loaded.tunables, live_model)
        live_result = decide(live_comparison.error_differences, live_comparison.execution_ids, valid=live_comparison.valid)
        if live_result.verdict == "promote" and coverage_regressed(live_comparison):
            live_result = replace(live_result, verdict="inconclusive", reason="coverage_regression")
        if candidate is not None and not candidate_invalid:
            candidate_model = self._with_pipeline(loaded.calibration, backend, pipeline, candidate)
            comparison = compare_replays(holdout, baseline_tunables, baseline_model, loaded.tunables, candidate_model)
            result = decide(comparison.error_differences, comparison.execution_ids, valid=comparison.valid)
            if result.verdict == "promote" and coverage_regressed(comparison):
                result = replace(result, verdict="inconclusive", reason="coverage_regression")
        else:
            comparison = None
            result = decide((), (), valid=not candidate_invalid) if candidate_was_fit else decide((), ())
        record["last_gate_execution"] = max(held_ids)
        candidate_ids = comparison.execution_ids if comparison is not None else ()
        record["last_gate_holdout_executions"] = len(candidate_ids)
        record["last_gate"] = {"candidate": result.verdict, "candidate_reason": result.reason, "live": live_result.verdict, "live_reason": live_result.reason, "holdout_executions": len(candidate_ids), "through_execution": max(held_ids)}
        report.gates[group] = result.verdict
        candidate_coverage = comparison.coverage_differences if comparison is not None else ()
        self._files.log(f"{group}: candidate {result.verdict} ({result.reason}; holdout={len(candidate_ids)}; observations={comparison.candidate.observations if comparison is not None else 0}; baseline_error={dict(comparison.baseline.errors) if comparison is not None else {}}; candidate_error={dict(comparison.candidate.errors) if comparison is not None else {}}; error_interval={result.interval}; baseline_coverage_distance={dict(comparison.baseline.coverage_distance) if comparison is not None else {}}; candidate_coverage_distance={dict(comparison.candidate.coverage_distance) if comparison is not None else {}}; coverage_difference={candidate_coverage}); live {live_result.verdict} ({live_result.reason}; holdout={len(live_comparison.execution_ids)}; observations={live_comparison.candidate.observations}; baseline_error={dict(live_comparison.baseline.errors)}; live_error={dict(live_comparison.candidate.errors)}; error_interval={live_result.interval}; baseline_coverage_distance={dict(live_comparison.baseline.coverage_distance)}; live_coverage_distance={dict(live_comparison.candidate.coverage_distance)}; coverage_difference={live_comparison.coverage_differences})")
        if result.verdict == "promote":
            assert candidate is not None
            saved = self._approval_without_pins(backend, pipeline, candidate, baseline, pins)
            if saved is None:
                report.gates[group] = "invalid_candidate"
                record["last_gate"]["candidate"] = "reject"
                record["last_gate"]["candidate_reason"] = "invalid_approval_without_pins"
                report.rejected.append(f"{group}: invalid approval without pins")
                return None
            approved[group] = saved
            record["approved_against"] = "learned" if group in baselines else "factory"
            return candidate
        if live_result.verdict == "reject":
            report.gates[group] = "restored"
            restored_tunables.append(approved_tunables.get(group, factory_tunables()))
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
                history = runs_of_store(self._store)
                self.last_report = self._engine.update(history, should_stop=lambda: self._stop.is_set() or not self._safe_quiet()) if isinstance(self._engine, KnobUpdateEngine) else self._engine.update(history)
            except Exception:
                logger.exception("The timing knob update could not read the store")
            if not self._wake.is_set():
                self._idle.set()

    def _safe_quiet(self) -> bool:
        try:
            return self._quiet()
        except Exception:
            return False
