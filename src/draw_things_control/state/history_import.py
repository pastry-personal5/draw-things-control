"""Import phase 1 job manifests into the state store."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from draw_things_control.core.clock import local_timestamp
from draw_things_control.core.numbers import positive_whole
from draw_things_control.jobs.events import JobStatus, RunStatus
from draw_things_control.state.executions import ExecutionSettings, NewExecution, NewRun, epoch
from draw_things_control.state.ids import EXECUTION_LETTER, execution_id_text, parse_typed_id
from draw_things_control.state.store import Store

MANIFEST_KEYS = ("job_file", "name", "mode", "seed", "started_at", "runs")


@dataclass
class ImportReport:
    """How many manifests were imported, already there, past the retention cutoff, or not manifests."""

    imported: int = 0
    skipped: int = 0
    expired: int = 0
    unreadable: int = 0
    # Each imported execution: the ID it was given, the ID its manifest records (None for one written before execution
    # IDs, or the same as given), and the manifest.
    given: list[tuple[str, str | None, Path]] = field(default_factory=list)


def import_history(store: Store, directory: Path, *, clock: datetime | None = None) -> ImportReport:
    """Insert every manifest under ``directory`` that the store does not have; never touch a manifest file."""
    report = ImportReport()
    now = local_timestamp(clock or datetime.now())
    cutoff = store.retention_cutoff()
    for path in _manifest_candidates(directory):
        key = str(path.resolve())
        try:
            manifest = _read_manifest(path)
            if store.executions.has_manifest(key):
                report.skipped += 1
                continue
            execution, runs, first_run = _convert(manifest, path, key, now)
            # A manifest left 'running' has no real finish time; judge it by its start, or each import would restamp it and bring back a pruned row.
            aged = execution.started_at if manifest.get("status", JobStatus.RUNNING) == JobStatus.RUNNING and not manifest.get("finished_at") else execution.finished_at or execution.started_at
            if cutoff is not None and epoch(aged) < cutoff:
                report.expired += 1
                continue
        except (OSError, ValueError, TypeError, KeyError):
            report.unreadable += 1
            continue
        _row, number = store.executions.import_execution(execution, runs, first_run=first_run)
        report.imported += 1
        # The next free number, whatever the start time; a manifest's own ID (its execution was pruned, or state/ was
        # deleted) is reported beside it, so the two can be matched, and never reused.
        recorded = manifest.get("execution_id")
        recorded_number = parse_typed_id(recorded, EXECUTION_LETTER) if isinstance(recorded, str) else None
        own = execution_id_text(recorded_number) if recorded_number is not None else None
        given = execution_id_text(number)
        report.given.append((given, own if own != given else None, path))
    return report


def _manifest_candidates(directory: Path) -> list[Path]:
    """Every visible ``*.json`` file under ``directory``, in a stable order."""
    found: list[Path] = []
    for root, names, files in os.walk(directory):
        names[:] = sorted(name for name in names if not name.startswith("."))
        found.extend(Path(root) / name for name in sorted(files) if name.endswith(".json") and not name.startswith("."))
    return found


def _read_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or any(key not in data for key in MANIFEST_KEYS) or not isinstance(data["runs"], list):
        raise ValueError(f"not a job manifest: {path}")
    return data


def _resume_fields(manifest: dict[str, Any]) -> tuple[int, int | None]:
    """The manifest's first run number (1, unless it is a resume) and the execution it resumes, if any."""
    first_run = positive_whole(manifest.get("first_run")) or 1
    resumed = manifest.get("resumes_execution")
    resumes = parse_typed_id(resumed, EXECUTION_LETTER) if isinstance(resumed, str) else None
    return first_run, resumes


def _settings(manifest: dict[str, Any]) -> ExecutionSettings:
    # Manifests from before the cooldown mapping have only cooldown_seconds, which means manual.
    cooldown = manifest["cooldown"] if isinstance(manifest.get("cooldown"), dict) else None
    return ExecutionSettings(
        config_file=manifest.get("config_file"),
        config_override=manifest.get("config_override"),
        input_resize=manifest.get("input_resize"),
        cooldown_seconds=manifest.get("cooldown_seconds"),
        cooldown_source=manifest.get("cooldown_source"),
        cooldown=cooldown,
    )


def _convert(manifest: dict[str, Any], path: Path, key: str, now: str) -> tuple[NewExecution, list[NewRun], int]:
    # A phase 1 process that crashed left its manifest 'running'; import it as the sweep would close it.
    stale = manifest.get("status", JobStatus.RUNNING) == JobStatus.RUNNING
    finished_at = manifest.get("finished_at") or (now if stale else None)
    log_file = manifest.get("log_file")
    # The manifest's per-run 'batch' field is ignored: the run number is its position in the list, offset by first_run
    # for a resumed manifest (whose list holds only the runs it made, from its own first run onward).
    first_run, resumes = _resume_fields(manifest)
    runs = [_convert_run(run) for run in manifest["runs"]]
    execution = NewExecution(
        job_name=str(manifest["name"]),
        job_file=str(manifest["job_file"]),
        mode=str(manifest["mode"]),
        status=JobStatus.INTERRUPTED if stale else str(manifest["status"]),
        seed=int(manifest["seed"]),
        seed_source=manifest.get("seed_source"),
        cooldown_seconds=manifest.get("cooldown_seconds"),
        cooldown_source=manifest.get("cooldown_source"),
        # The manifest's own total_runs (the whole chain, for a resumed manifest whose runs list holds only its own);
        # a manifest written before that field existed has none, and len(runs) is then the whole chain anyway.
        total_runs=positive_whole(manifest.get("total_runs")) or len(runs),
        started_at=str(manifest["started_at"]),
        finished_at=finished_at,
        manifest_path=key,
        log_path=str(path.parent / log_file) if log_file else None,
        config_file=manifest.get("config_file"),
        recovered_at=now if stale else None,
        first_run=first_run,
        resumes=resumes,
        settings=_settings(manifest),
        # Written since Milestone 09; older manifests have none.
        first_image=_text(manifest.get("first_image")),
    )
    epoch(execution.started_at)
    if finished_at is not None:
        epoch(finished_at)
    return execution, runs, first_run


def _text(value: Any) -> str | None:
    """A manifest's optional file name or path; anything but non-empty text reads as none."""
    return value if isinstance(value, str) and value else None


def _convert_run(run: dict[str, Any]) -> NewRun:
    status = run.get("status", RunStatus.RUNNING)
    converted = NewRun(
        pair=str(run["pair"]),
        positive=str(run["positive"]),
        negative=run.get("negative"),
        input=run.get("input"),
        resized_input=run.get("resized_input"),
        output=run.get("output"),
        last_frame=run.get("last_frame"),
        command=list(run.get("command") or []),
        started_at=str(run["started_at"]),
        seconds=run.get("seconds"),
        exit_code=run.get("exit_code"),
        status=RunStatus.INTERRUPTED if status == RunStatus.RUNNING else str(status),
        cooldown_after_seconds=run.get("cooldown_after_seconds"),
        # Measured sizes, from manifests written since they were recorded; older ones have none.
        output_width=positive_whole(run.get("output_width")),
        output_height=positive_whole(run.get("output_height")),
        output_frames=positive_whole(run.get("output_frames")),
        anchor=_text(run.get("anchor")),
        corrected_output=_text(run.get("corrected_output")),
    )
    epoch(converted.started_at)
    return converted
