"""Import phase 1 job manifests into the state store."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from draw_things_control.state.store import Store, epoch

MANIFEST_KEYS = ("job_file", "name", "mode", "seed", "started_at", "runs")


@dataclass
class ImportReport:
    """How many manifests were imported, already there, past the retention cutoff, or not manifests."""

    imported: int = 0
    skipped: int = 0
    expired: int = 0
    unreadable: int = 0


def import_history(store: Store, directory: Path, *, clock: datetime | None = None) -> ImportReport:
    """Insert every manifest under ``directory`` that the store does not have; never touch a manifest file."""
    report = ImportReport()
    now = (clock or datetime.now()).astimezone().isoformat(timespec="seconds")
    cutoff = store.retention_cutoff()
    for path in _manifest_candidates(directory):
        key = str(path.resolve())
        try:
            manifest = _read_manifest(path)
            if store.has_manifest(key):
                report.skipped += 1
                continue
            execution, runs = _convert(manifest, path, key, now)
            # A manifest left 'running' has no real finish time; judge it by its start, or each import would restamp it and bring back a pruned row.
            aged = execution["started_at"] if manifest.get("status", "running") == "running" and not manifest.get("finished_at") else execution["finished_at"] or execution["started_at"]
            if cutoff is not None and epoch(aged) < cutoff:
                report.expired += 1
                continue
        except (OSError, ValueError, TypeError, KeyError):
            report.unreadable += 1
            continue
        store.import_execution(execution, runs)
        report.imported += 1
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


def _convert(manifest: dict[str, Any], path: Path, key: str, now: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    # A phase 1 process that crashed left its manifest 'running'; import it as the sweep would close it.
    stale = manifest.get("status", "running") == "running"
    finished_at = manifest.get("finished_at") or (now if stale else None)
    log_file = manifest.get("log_file")
    execution = {
        "job_name": str(manifest["name"]),
        "job_file": str(manifest["job_file"]),
        "mode": str(manifest["mode"]),
        "status": "interrupted" if stale else str(manifest["status"]),
        "seed": int(manifest["seed"]),
        "seed_source": manifest.get("seed_source"),
        "cooldown_seconds": manifest.get("cooldown_seconds"),
        "cooldown_source": manifest.get("cooldown_source"),
        "total_runs": len(manifest["runs"]),
        "started_at": str(manifest["started_at"]),
        "finished_at": finished_at,
        "manifest_path": key,
        "log_path": str(path.parent / log_file) if log_file else None,
        "config_file": manifest.get("config_file"),
        "recovered_at": now if stale else None,
        "settings": {"config_file": manifest.get("config_file"), "config_override": manifest.get("config_override"), "input_resize": manifest.get("input_resize"), "cooldown_seconds": manifest.get("cooldown_seconds"), "cooldown_source": manifest.get("cooldown_source")},
    }
    # Manifests from before the cooldown mapping have only cooldown_seconds, which means manual.
    if isinstance(manifest.get("cooldown"), dict):
        execution["settings"]["cooldown"] = manifest["cooldown"]
    epoch(execution["started_at"])
    if finished_at is not None:
        epoch(finished_at)
    # The manifest's per-run 'batch' field is ignored: the run number is the position in the list.
    runs = [_convert_run(run) for run in manifest["runs"]]
    return execution, runs


def _convert_run(run: dict[str, Any]) -> dict[str, Any]:
    status = run.get("status", "running")
    converted = {
        "pair": str(run["pair"]),
        "positive": str(run["positive"]),
        "negative": run.get("negative"),
        "input": run.get("input"),
        "resized_input": run.get("resized_input"),
        "output": run.get("output"),
        "last_frame": run.get("last_frame"),
        "command": list(run.get("command") or []),
        "started_at": str(run["started_at"]),
        "seconds": run.get("seconds"),
        "exit_code": run.get("exit_code"),
        "status": "interrupted" if status == "running" else str(status),
        "cooldown_after_seconds": run.get("cooldown_after_seconds"),
    }
    epoch(converted["started_at"])
    return converted
