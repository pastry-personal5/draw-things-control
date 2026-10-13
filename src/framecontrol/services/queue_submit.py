"""Submit a job to the persistent queue: validate it and snapshot exactly what was validated, so editing or deleting
the job file, its base configuration, or the global configuration afterwards changes nothing about what runs."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from framecontrol.core.clock import Clock, local_timestamp
from framecontrol.core.cooldown import CooldownPolicy, parse_cooldown
from framecontrol.core.errors import FramecontrolError, InputError
from framecontrol.core.global_config import GlobalConfig
from framecontrol.core.yaml_files import read_yaml_file
from framecontrol.jobs.backend import BackendRegistry
from framecontrol.jobs.backends import DEFAULT_BACKENDS
from framecontrol.jobs.backends.h3_references import cleanup_snapshot, snapshot_references, verify_snapshot
from framecontrol.jobs.definition import JobDefinition
from framecontrol.jobs.parsing import load_job_text
from framecontrol.state.executions import ExecutionSettings
from framecontrol.state.queue import NewQueueEntry, QueueRow
from framecontrol.state.store import Store

# A worker's own insert-and-publish method (``QueueWorker.enqueue``), threaded through so the actual DB insert and
# its 'queued' event happen under the worker's own claim lock, and can therefore never interleave with a claim (a
# plain ``store.queue.submit`` call here would let the worker claim the entry and publish 'running' before this
# publishes 'queued': Milestone 02's event-order fix, see routes_queue.py and queue_worker.py). A plain type alias,
# not ``QueueWorker`` itself: ``queue_worker.py`` already imports this module for ``parse_snapshot``, and importing
# back would cycle.
Enqueue = Callable[[Callable[[], QueueRow]], QueueRow]


def submit_job(job_path: Path, global_config: GlobalConfig, params_directory: Path, store: Store, *, clock: Clock = datetime.now, before_submit: Callable[[JobDefinition], None] | None = None, enqueue: Enqueue | None = None, submitted_by: str | None = None, backends: BackendRegistry = DEFAULT_BACKENDS) -> QueueRow:
    """Validate ``job_path`` exactly as running it would, then store its snapshot as a new ``queued`` entry.

    Refuses (raises whatever ``load_job_text`` raises, an ``InputError``) before anything is stored: an invalid job,
    a missing input file, or a missing base configuration.

    ``before_submit``, when given, is called with the job parsed from the exact text about to be stored (Milestone
    02's own API rules and limits), so what it checks is what runs, not an earlier read of a file that may have
    changed since, and before the input image is decoded; it raising refuses the submission and stores nothing.
    ``enqueue`` does the actual insert and publish, as described above; None (most tests) inserts directly.
    ``submitted_by`` is the request's caller, which the entry records (Milestone 10).
    """
    path = job_path.expanduser().resolve()
    try:
        _data, job_text = read_yaml_file(path, "Job file", max_bytes=global_config.api_limits.max_job_file_bytes)
    except ValueError as error:
        if isinstance(error, InputError):
            raise
        raise InputError(f"{path}: {error}", path=path) from error
    # First parsed from disk, to learn the base configuration's name. The input is not decoded yet: before_submit's
    # rules (the input inside the input directory among them) refuse a job before any image it names is decoded.
    job = load_job_text(job_text, path, global_config, params_directory, decode_input=False, backends=backends)
    if before_submit is not None:
        before_submit(job)
    try:
        # Capture the backend's source dependency after the first parse, then validate the exact captured text.
        config_text = backends.get(job.backend).snapshot_config(job, params_directory, max(1048576, global_config.api_limits.max_job_file_bytes))
    except (ValueError, OSError) as error:
        if isinstance(error, FramecontrolError):
            raise
        raise InputError(f"{path}: {error}") from error
    # Re-parsed from the exact text about to be stored, so a base configuration edited between the two reads above
    # cannot be captured half-written: what is stored is validated in the form it is stored, the input decoded
    # included, not merely read twice.
    job = load_job_text(job_text, path, global_config, params_directory, decode_input=True, base_config_text=config_text, backends=backends)
    try:
        _snapshot_root, reference_assets = snapshot_references(job, store.path.parent / "queue-inputs", global_config.api_limits.max_reference_file_bytes, global_config.api_limits.max_reference_bytes)
    except (OSError, ValueError) as error:
        raise InputError(f"{path}: {error}") from error
    if reference_assets:
        try:
            overrides = verify_snapshot(reference_assets)
            copied_job = load_job_text(job_text, path, global_config, params_directory, decode_input=True, base_config_text=config_text, reference_overrides=overrides, require_reference_snapshots=True, backends=backends)
        except (OSError, ValueError) as error:
            cleanup_snapshot(reference_assets)
            if isinstance(error, FramecontrolError):
                raise
            raise InputError(f"{path}: copied H3 references failed verification: {error}") from error
        if copied_job.backend_settings != job.backend_settings:
            cleanup_snapshot(reference_assets)
            raise InputError(f"{path}: H3 reference settings changed while snapshotting", field="reference_pairs", path=path)
        job = copied_job
    try:
        new = _new_submitted_entry(path, job_text, config_text, job, global_config, local_timestamp(clock()), submitted_by, reference_assets)
        return enqueue(lambda: store.queue.submit(new)) if enqueue is not None else store.queue.submit(new)
    except BaseException:
        if reference_assets:
            cleanup_snapshot(reference_assets)
        raise


def _new_submitted_entry(path: Path, job_text: str, config_text: str, job: JobDefinition, global_config: GlobalConfig, submitted_at: str, submitted_by: str | None, reference_assets: list[dict[str, object]] | None = None) -> NewQueueEntry:
    snapshot = {"version": 6 if job.backend == "h3" else 4 if reference_assets else 3, "kind": "job", "backend": job.backend, "job_path": str(path), "job_text": job_text, "config_file": job.config_file, "config_text": config_text, "input_directory": str(global_config.input_directory), "output_directory": str(global_config.output_directory), "cooldown_default": global_config.cooldown.as_dict() if global_config.cooldown is not None else None, "settings": json.loads(_execution_settings(job).to_json())}
    if reference_assets:
        snapshot["reference_assets"] = reference_assets
    return NewQueueEntry(
        job_path=str(path),
        job_text=job_text,
        config_file=job.config_file,
        config_text=config_text,
        input_directory=str(global_config.input_directory),
        output_directory=str(global_config.output_directory),
        cooldown_default=global_config.cooldown.as_dict() if global_config.cooldown is not None else None,
        settings=_execution_settings(job),
        submitted_at=submitted_at,
        total_runs=job.run_count,
        submitted_by=submitted_by,
        backend=job.backend,
        snapshot=json.dumps(snapshot, ensure_ascii=False),
    )


def _execution_settings(job: JobDefinition) -> ExecutionSettings:
    """The settings a submitted job resolved to, shown to clients: the same shape ``ExecutionRecorder`` keeps."""
    return ExecutionSettings(
        input=str(job.input) if job.input is not None else None,
        output_directory=str(job.output_directory),
        cooldown_seconds=job.cooldown.fixed_seconds,
        cooldown_source=job.cooldown_source,
        cooldown=job.cooldown.as_dict(),
        config_file=job.config_file,
        config_override=job.config_override.as_dict(),
        input_resize=job.input_resize.as_manifest() if job.input_resize is not None else None,
        backend=job.backend,
        backend_settings=dict(job.backend_settings),
    )


def global_config_of(entry: QueueRow, live: GlobalConfig) -> GlobalConfig:
    """The global configuration a queued entry's job parses against: its own snapshot for ``input_directory``,
    ``output_directory``, and the cooldown default, and the live configuration for everything else (``write_job_records``,
    ``history_retention_days``), which are session settings, not part of what a job resolves to."""
    cooldown = _cooldown_of(entry.cooldown_default) if entry.cooldown_default is not None else None
    return GlobalConfig(
        input_directory=Path(entry.input_directory),
        output_directory=Path(entry.output_directory),
        write_job_records=live.write_job_records,
        cooldown=cooldown,
        history_retention_days=live.history_retention_days,
    )


def _cooldown_of(data: dict[str, object]) -> CooldownPolicy:
    # ``data`` is CooldownPolicy.as_dict()'s own output, captured at submission, so it is always valid; parse_cooldown
    # is reused rather than unpacking the mapping by hand, which needs no type-ignore for a "mode" already popped.
    return parse_cooldown(data, "cooldown")


def _legacy_h3_frames(snapshot: dict[str, object]) -> int | None:
    """Preserve the upward alignment of H3 jobs submitted before snapshot v6."""
    if snapshot.get("backend") != "h3" or snapshot.get("version") not in (3, 4, 5):
        return None
    settings = snapshot.get("settings")
    backend = settings.get("backend_settings") if isinstance(settings, dict) else None
    if not isinstance(backend, dict):
        return None
    requested, stored = backend.get("requested_frames"), backend.get("effective_frames")
    if type(requested) is not int or type(stored) is not int or not 24 <= requested <= 360:
        return None
    old = min(362, 5 + 17 * math.ceil(max(0, requested - 5) / 17))
    return old if stored == old else None


def parse_snapshot(entry: QueueRow, live: GlobalConfig, params_directory: Path, *, decode_input: bool = True, allow_missing_input: bool = False, first_run: int | None = None, backends: BackendRegistry = DEFAULT_BACKENDS) -> Callable[[], JobDefinition]:
    """A thunk that parses the entry's snapshot into a ``JobDefinition``, exactly as it was validated at submission.
    ``params_directory`` only names the base configuration in messages: its text comes from the snapshot, never disk.
    ``decode_input=False`` for a caller that only reads the parsed job's fields (not running it), to skip decoding
    the input image a second time when it was already fully decoded moments earlier for the same request."""

    def parse() -> JobDefinition:
        if entry.snapshot is not None:
            try:
                snapshot = json.loads(entry.snapshot)
            except (TypeError, ValueError) as error:
                raise InputError(f"{entry.label}: invalid queue snapshot") from error
            if not isinstance(snapshot, dict) or snapshot.get("kind") != "job" or snapshot.get("version") not in (1, 2, 3, 4, 5, 6):
                raise InputError(f"{entry.label}: unsupported job snapshot")
            if snapshot["version"] in (2, 3, 4, 5, 6):
                if snapshot.get("backend") != entry.backend:
                    raise InputError(f"{entry.label}: backend differs between the queue row and snapshot", field="backend")
                if not all(isinstance(snapshot.get(key), str) for key in ("job_path", "job_text", "config_file", "config_text", "input_directory", "output_directory")):
                    raise InputError(f"{entry.label}: incomplete job snapshot")
                for key, stored in (("job_path", entry.job_path), ("job_text", entry.job_text), ("config_file", entry.config_file), ("config_text", entry.config_text), ("input_directory", entry.input_directory), ("output_directory", entry.output_directory)):
                    if snapshot[key] != stored:
                        raise InputError(f"{entry.label}: {key} differs between the queue row and snapshot", field=key)
                if snapshot.get("cooldown_default") != entry.cooldown_default or snapshot.get("settings") != json.loads(entry.settings.to_json()):
                    raise InputError(f"{entry.label}: settings differ between the queue row and snapshot", field="settings")
                config = GlobalConfig(input_directory=Path(snapshot["input_directory"]), output_directory=Path(snapshot["output_directory"]), write_job_records=live.write_job_records, cooldown=_cooldown_of(snapshot["cooldown_default"]) if snapshot.get("cooldown_default") is not None else None, history_retention_days=live.history_retention_days)
                reference_assets = snapshot.get("reference_assets", []) if snapshot["version"] in (4, 5, 6) else []
                if not isinstance(reference_assets, list) or any(not isinstance(item, dict) for item in reference_assets):
                    raise InputError(f"{entry.label}: invalid reference asset snapshot")
                if snapshot["version"] == 4 and not reference_assets:
                    raise InputError(f"{entry.label}: missing H3 reference assets")
                reference_facts: dict[tuple[str, int, str], dict[str, object]] = {}
                for item in reference_assets:
                    if not isinstance(item.get("pair"), str) or not isinstance(item.get("ordinal"), int) or item.get("field") not in ("path", "audio") or not isinstance(item.get("snapshot_path"), str) or not isinstance(item.get("sha256"), str) or not isinstance(item.get("size"), int) or not isinstance(item.get("media_facts"), dict):
                        raise InputError(f"{entry.label}: invalid reference asset descriptor")
                    key = (item["pair"], item["ordinal"], item["field"])
                    if key in reference_facts:
                        raise InputError(f"{entry.label}: duplicate reference asset descriptor")
                    reference_facts[key] = item
                inactive_pairs: frozenset[str] = frozenset()
                start = first_run if first_run is not None else entry.resume_first_run or 1
                if start > 1 and reference_assets:
                    settings = snapshot["settings"].get("backend_settings")
                    runs = settings.get("h3_runs") if isinstance(settings, dict) else None
                    if not isinstance(runs, list):
                        raise InputError(f"{entry.label}: missing H3 run plan in reference snapshot")
                    active = {run.get("reference_pair") for run in runs if isinstance(run, dict) and isinstance(run.get("run"), int) and run["run"] >= start and run.get("pipeline") == "r2av"}
                    inactive_pairs = frozenset(item["pair"] for item in reference_assets if item["pair"] not in active)
                try:
                    verify_snapshot([item for item in reference_assets if item["pair"] not in inactive_pairs])
                except ValueError as error:
                    raise InputError(f"{entry.label}: {error}") from error
                reference_overrides = {key: Path(str(item["snapshot_path"])) for key, item in reference_facts.items()}
                job = load_job_text(snapshot["job_text"], Path(snapshot["job_path"]), config, params_directory, decode_input=decode_input, base_config_text=snapshot["config_text"], allow_missing_input=allow_missing_input, reference_overrides=reference_overrides, require_reference_snapshots=bool(reference_assets), inactive_reference_pairs=inactive_pairs, reference_facts=reference_facts, legacy_h3_frames=_legacy_h3_frames(snapshot), backends=backends)
                if not reference_assets and job.backend == "h3":
                    from framecontrol.jobs.backends.h3 import H3JobData

                    if isinstance(job.backend_data, H3JobData) and job.backend_data.reference_pairs:
                        raise InputError(f"{entry.label}: H3 references require a reference asset snapshot")
                if job.backend != entry.backend:
                    raise InputError(f"{entry.label}: backend differs between job text and queue snapshot", field="backend")
                if snapshot["version"] in (3, 4, 5, 6):
                    validated_settings = json.loads(_execution_settings(job).to_json())
                    if snapshot["version"] in (3, 4) and job.backend == "h3":
                        validated_settings["backend_settings"].pop("color_policy", None)
                    if validated_settings != snapshot["settings"]:
                        raise InputError(f"{entry.label}: validated settings differ from the queue snapshot", field="settings")
                    if entry.total_runs is not None and job.run_count != entry.total_runs:
                        raise InputError(f"{entry.label}: run count differs from the queue row", field="run_count")
                return job
        global_config = global_config_of(entry, live)
        job = load_job_text(entry.job_text, Path(entry.job_path), global_config, params_directory, decode_input=decode_input, base_config_text=entry.config_text, allow_missing_input=allow_missing_input, backends=backends)
        if job.backend != entry.backend:
            raise InputError(f"{entry.label}: backend differs between job text and queue row", field="backend")
        return job

    return parse
