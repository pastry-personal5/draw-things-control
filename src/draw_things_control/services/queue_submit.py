"""Submit a job to the persistent queue: validate it and snapshot exactly what was validated, so editing or deleting
the job file, its base configuration, or the global configuration afterwards changes nothing about what runs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from draw_things_control.core.clock import Clock, local_timestamp
from draw_things_control.core.cooldown import CooldownPolicy, parse_cooldown
from draw_things_control.core.draw_things_config import find_config_file
from draw_things_control.core.errors import DtcError, InputError
from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.core.yaml_files import read_bounded_bytes, read_yaml_file
from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.jobs.parsing import load_job_text
from draw_things_control.state.executions import ExecutionSettings
from draw_things_control.state.queue import NewQueueEntry, QueueRow
from draw_things_control.state.store import Store

# A worker's own insert-and-publish method (``QueueWorker.enqueue``), threaded through so the actual DB insert and
# its 'queued' event happen under the worker's own claim lock, and can therefore never interleave with a claim (a
# plain ``store.queue.submit`` call here would let the worker claim the entry and publish 'running' before this
# publishes 'queued': Milestone 02's event-order fix, see routes_queue.py and queue_worker.py). A plain type alias,
# not ``QueueWorker`` itself: ``queue_worker.py`` already imports this module for ``parse_snapshot``, and importing
# back would cycle.
Enqueue = Callable[[Callable[[], QueueRow]], QueueRow]


def submit_job(job_path: Path, global_config: GlobalConfig, params_directory: Path, store: Store, *, clock: Clock = datetime.now, before_submit: Callable[[JobDefinition], None] | None = None, enqueue: Enqueue | None = None, submitted_by: str | None = None) -> QueueRow:
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
    job = load_job_text(job_text, path, global_config, params_directory, decode_input=False)
    if before_submit is not None:
        before_submit(job)
    try:
        # A plain ValueError (a bad name) or OSError (removed, or unreadable, between the two reads) is wrapped:
        # every refusal here is an InputError, as load_job_text's own base-config reads already are.
        config_path = find_config_file(job.config_file, params_directory)
        config_text = read_bounded_bytes(config_path, max(1048576, global_config.api_limits.max_job_file_bytes), "Configuration", key="config_file").decode("utf-8")
    except (ValueError, OSError) as error:
        if isinstance(error, DtcError):
            raise
        raise InputError(f"{path}: {error}") from error
    # Re-parsed from the exact text about to be stored, so a base configuration edited between the two reads above
    # cannot be captured half-written: what is stored is validated in the form it is stored, the input decoded
    # included, not merely read twice.
    job = load_job_text(job_text, path, global_config, params_directory, decode_input=True, base_config_text=config_text)
    new = _new_submitted_entry(path, job_text, config_text, job, global_config, local_timestamp(clock()), submitted_by)
    return enqueue(lambda: store.queue.submit(new)) if enqueue is not None else store.queue.submit(new)


def _new_submitted_entry(path: Path, job_text: str, config_text: str, job: JobDefinition, global_config: GlobalConfig, submitted_at: str, submitted_by: str | None) -> NewQueueEntry:
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


def parse_snapshot(entry: QueueRow, live: GlobalConfig, params_directory: Path, *, decode_input: bool = True) -> Callable[[], JobDefinition]:
    """A thunk that parses the entry's snapshot into a ``JobDefinition``, exactly as it was validated at submission.
    ``params_directory`` only names the base configuration in messages: its text comes from the snapshot, never disk.
    ``decode_input=False`` for a caller that only reads the parsed job's fields (not running it), to skip decoding
    the input image a second time when it was already fully decoded moments earlier for the same request."""

    def parse() -> JobDefinition:
        global_config = global_config_of(entry, live)
        return load_job_text(entry.job_text, Path(entry.job_path), global_config, params_directory, decode_input=decode_input, base_config_text=entry.config_text)

    return parse
