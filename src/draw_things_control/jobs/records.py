"""What a job leaves beside its outputs: a JSON manifest of every run, and its log file."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from draw_things_control.core.clock import Clock, local_timestamp
from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.jobs.events import JobStatus, RunStatus
from draw_things_control.jobs.output_naming import RandomNumber, job_file_stem

if TYPE_CHECKING:
    from loguru import Record


@dataclass
class RunRecord:
    """One run: its prompts, files, command, and result."""

    pair: str
    positive: str
    negative: str | None
    input: str | None
    output: str | None
    last_frame: str | None
    command: list[str]
    started_at: str
    seconds: float | None = None
    exit_code: int | None = None
    status: RunStatus = RunStatus.RUNNING
    # Seconds the job waited after this run, before the next; None when it did not wait.
    cooldown_after_seconds: float | None = None
    # Run 1's resized copy of ``input``, which ``command`` passes as --image; removed after the run.
    # Rebuild it from the job manifest's ``input_resize`` to replay the command.
    resized_input: str | None = None
    # The output file's actual size and frame count, measured after a successful run; None when unknown.
    output_width: int | None = None
    output_height: int | None = None
    output_frames: int | None = None


@dataclass
class JobManifest:
    """A job's settings and the record of every run so far."""

    job_file: str
    name: str
    mode: str
    config_file: str
    config_override: dict[str, Any]
    seed: int
    seed_source: str
    # The manual wait, 0 for off, or None for auto; ``cooldown`` is the whole mapping as resolved.
    cooldown_seconds: float | None
    cooldown_source: str
    cooldown: dict[str, Any] | None
    started_at: str
    log_file: str | None
    # The execution's ID in the state store (E0012); None in a manifest written before execution IDs, or by a job run
    # without one.
    execution_id: str | None = None
    input_resize: dict[str, Any] | None = None
    finished_at: str | None = None
    status: JobStatus = JobStatus.RUNNING
    runs: list[RunRecord] = field(default_factory=list)


def write_manifest(path: Path, manifest: JobManifest) -> None:
    """Write the manifest atomically, so a crash never leaves a partial file."""
    text = json.dumps(asdict(manifest), indent=2, ensure_ascii=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _format(record: Record) -> str:
    # Child lines already start with "[stdout +1.2s]" or "[stderr +1.2s]".
    stream = "" if record["extra"].get("child_stream") else "[app] "
    return "{time:YYYY-MM-DD HH:mm:ss.SSS} " + stream + "{message}\n{exception}"


def add_job_log(path: Path) -> int:
    """Start copying every log message to ``path``; returns the sink id."""
    return logger.add(path, format=_format, level="INFO", colorize=False, buffering=1, encoding="utf-8")


def remove_job_log(sink_id: int) -> None:
    """Stop writing to the job log and close it."""
    logger.remove(sink_id)


class JobRecords:
    """A job's manifest and, when records are written, its files: the manifest and the log, both beside the outputs.

    Use ``open`` around a job: it creates the output directory and the log (which copies every log message of the job,
    the child's too, until the block ends), and, when the job raises, marks a manifest still ``running`` as failed.
    """

    def __init__(self, manifest: JobManifest, manifest_path: Path | None, log_path: Path | None) -> None:
        self.manifest = manifest
        # Both None unless records are written beside the outputs.
        self.manifest_path = manifest_path
        self.log_path = log_path

    def save(self) -> None:
        """Write the manifest, when records are written."""
        if self.manifest_path is not None:
            write_manifest(self.manifest_path, self.manifest)

    @classmethod
    @contextmanager
    def open(cls, job: JobDefinition, *, write_records: bool, seed: int, seed_source: str, execution_id: str | None, clock: Clock, random_number: RandomNumber) -> Iterator[JobRecords]:
        job.output_directory.mkdir(parents=True, exist_ok=True)
        manifest_path: Path | None = None
        log_path: Path | None = None
        log_sink: int | None = None
        manifest: JobManifest | None = None
        try:
            if write_records:
                stem = job_file_stem(job.output_directory, job.name, clock, random_number)
                manifest_path = job.output_directory / f"{stem}.json"
                log_path = job.output_directory / f"{stem}.log"
                log_sink = add_job_log(log_path)
            manifest = JobManifest(
                job_file=str(job.path),
                name=job.name,
                mode=str(job.mode),
                config_file=job.config_file,
                config_override=job.config_override.as_dict(),
                seed=seed,
                seed_source=seed_source,
                cooldown_seconds=job.cooldown.fixed_seconds,
                cooldown_source=job.cooldown_source,
                cooldown=job.cooldown.as_dict(),
                started_at=local_timestamp(clock()),
                log_file=log_path.name if log_path is not None else None,
                execution_id=execution_id,
                input_resize=job.input_resize.as_manifest() if job.input_resize is not None else None,
            )
            records = cls(manifest, manifest_path, log_path)
            yield records
        except BaseException:
            if manifest is not None and manifest.status == JobStatus.RUNNING:
                manifest.status = JobStatus.FAILED
                manifest.finished_at = local_timestamp(clock())
                if manifest_path is not None:
                    write_manifest(manifest_path, manifest)
            raise
        finally:
            if log_sink is not None:
                remove_job_log(log_sink)
