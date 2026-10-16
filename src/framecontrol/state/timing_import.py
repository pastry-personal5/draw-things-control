"""Rebuild timing rows from the job logs that still exist, for executions recorded before runs were timed as they ran."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from framecontrol.core.errors import InputError
from framecontrol.jobs.backend import BackendRegistry
from framecontrol.jobs.backends import DEFAULT_BACKENDS
from framecontrol.jobs.timing.log_import import LoggedRun, parse_job_log
from framecontrol.jobs.timing.parsers import StageParser
from framecontrol.state.execution_rows import ExecutionRow, RunRow
from framecontrol.state.store import Store
from framecontrol.state.timing import LOG

PAGE = 200


@dataclass
class TimingImportReport:
    """What the import did: the executions and runs given timing, and each execution or run it left alone, with why."""

    executions: int = 0
    runs: int = 0
    # (an execution's ID such as E0031, or a run such as "E0031 run 2", and the reason).
    skipped: list[tuple[str, str]] = field(default_factory=list)


def import_timing(store: Store, backends: BackendRegistry = DEFAULT_BACKENDS) -> TimingImportReport:
    """Give timing to every execution that has none and whose log still exists, marked ``log`` fidelity. An execution with
    any timing row (recorded live, or by an earlier import) is left alone, so the import is idempotent and never shifts a
    log that was already stamped as read. A log is only read."""
    report = TimingImportReport()
    offset = 0
    while True:
        page = store.executions.page(limit=PAGE, offset=offset)
        if not page:
            return report
        offset += len(page)
        for summary in page:
            _import_execution(store, summary, backends, report)


def _import_execution(store: Store, summary: ExecutionRow, backends: BackendRegistry, report: TimingImportReport) -> None:
    label = summary.label
    reason = _unusable(store, summary)
    if reason is not None:
        report.skipped.append((label, reason))
        return
    assert summary.log_path is not None
    try:
        text = Path(summary.log_path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        report.skipped.append((label, "its log no longer exists"))
        return
    except OSError as error:
        report.skipped.append((label, f"its log cannot be read: {error.strerror or error}"))
        return
    execution = store.executions.get(summary.id)
    assert execution is not None
    logged = parse_job_log(text, lambda number: _parser(execution, number, backends))
    if not logged:
        report.skipped.append((label, "its log holds no run"))
        return
    imported = 0
    for run in execution.runs:
        found = logged.get(run.number)
        if found is None:
            report.skipped.append((f"{label} run {run.number}", "its log does not hold the run"))
            continue
        _keep(store, execution, run, found, backends)
        imported += 1
    if imported:
        report.executions += 1
        report.runs += imported


def _unusable(store: Store, summary: ExecutionRow) -> str | None:
    """Why an execution's log is not read, or None when it should be."""
    if store.timing.has_timing(summary.id):
        return "it already has timing"
    if summary.status == "running":
        return "it is still running"
    if not summary.log_path:
        return "no log was written"
    if Path(summary.log_path).suffix != ".log":
        return "its log path is not a .log file"
    return None


def _parser(execution: ExecutionRow, number: int, backends: BackendRegistry) -> StageParser | None:
    """The parser of run ``number``'s output: its backend's, if the backend has one."""
    run = next((candidate for candidate in execution.runs if candidate.number == number), None)
    backend_id = (run.backend if run is not None else None) or execution.backend
    try:
        make = getattr(backends.get(backend_id), "stage_parser", None)
    except InputError:
        return None
    return make() if make is not None else None


def _backend_id(backend: str, backends: BackendRegistry) -> str:
    """The backend's own id, whichever spelling the run recorded."""
    try:
        return backends.get(backend).id
    except InputError:
        return backend


def _keep(store: Store, execution: ExecutionRow, run: RunRow, found: LoggedRun, backends: BackendRegistry) -> None:
    store.timing.replace_run(
        execution.id,
        run.number,
        backend=_backend_id(run.backend or execution.backend, backends),
        pipeline=run.pipeline or execution.mode,
        workload=_workload(execution, run, found),
        environment={},
        fidelity=LOG,
        complete=run.status == "succeeded" and found.finished,
        spans=found.spans,
    )


def _workload(execution: ExecutionRow, run: RunRow, found: LoggedRun) -> dict[str, Any]:
    """What the run is known to have done: its measured size and frames, the steps its output counted, and its knobs."""
    workload: dict[str, Any] = {"width": run.output_width, "height": run.output_height, "frames": run.output_frames}
    if run.output_width is not None and run.output_height is not None:
        workload["pixels"] = run.output_width * run.output_height
    steps = next((span.total for span in found.spans if span.unit == "steps" and span.total), None)
    if steps is not None:
        workload["steps"] = steps
    knobs = {name: value for name, value in run.knobs.items() if name != "seed"}
    if knobs:
        workload["knobs"] = knobs
    return {name: value for name, value in workload.items() if value is not None}


__all__ = ["TimingImportReport", "import_timing"]
