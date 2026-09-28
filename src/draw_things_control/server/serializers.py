"""Turns the package's own dataclasses into the API's JSON-safe response bodies. Commands were redacted when
stored, and are redacted again as they are served (``redact_command``), so an older row cannot leak a credential."""

from __future__ import annotations

from typing import Any

from draw_things_control.core.arguments import redact_command
from draw_things_control.jobs.definition import JobDefinition, PromptPair
from draw_things_control.jobs.planning import JobPreview
from draw_things_control.services.input_listing import InputImage
from draw_things_control.services.job_catalog import JobRow
from draw_things_control.state.audit import AuditRow
from draw_things_control.state.execution_rows import ExecutionRow, RunRow
from draw_things_control.state.ids import execution_id_text, queue_id_text
from draw_things_control.state.queue import QueueRow


def job_summary(row: JobRow) -> dict[str, Any]:
    """One row of ``GET /jobs``: job ID, file name, job name, mode, runs, and validity or the first error and field."""
    base = {"job_id": row.job_id, "file_name": row.path.name}
    if row.job is not None:
        return {**base, "name": row.job.name, "mode": row.job.mode.value, "runs": row.job.run_count, "valid": True, "error": None, "field": None}
    return {**base, "name": None, "mode": None, "runs": None, "valid": False, "error": row.error, "field": row.error_field}


def prompt_pair(pair: PromptPair) -> dict[str, Any]:
    return {"name": pair.name, "positive": pair.positive, "negative": pair.negative, "runs": list(pair.runs), "default": pair.default}


def job_detail(job: JobDefinition, source_text: str) -> dict[str, Any]:
    """``GET /jobs/{job}``: the file's text, and the resolved job (prompt pairs, size, seed, cooldown and their
    sources)."""
    seed, seed_source = job.configured_seed()
    return {
        "text": source_text,
        "name": job.name,
        "mode": job.mode.value,
        "input": str(job.input) if job.input is not None else None,
        "run_count": job.run_count,
        "prompt_pairs": [prompt_pair(pair) for pair in job.prompt_pairs],
        "output_directory": str(job.output_directory),
        "extension": job.extension,
        "config_file": job.config_file,
        "config_override": job.config_override.as_dict(),
        "model": job.model,
        "run_timeout_seconds": job.run_timeout_seconds,
        "size": list(job.size) if job.size is not None else None,
        "seed": seed,
        "seed_source": seed_source,
        "cooldown": job.cooldown.as_dict(),
        "cooldown_source": job.cooldown_source,
    }


def job_preview(job: JobDefinition, preview: JobPreview) -> dict[str, Any]:
    """``GET /jobs/{job}/preview``: the dry-run plan, commands redacted."""
    return {
        "seed": preview.seed,
        "seed_source": preview.seed_source,
        "runs": [{"number": run.number, "pair": run.pair.name, "input": str(run.input) if run.input is not None else None, "output": str(run.output), "last_frame": str(run.last_frame) if run.last_frame is not None else None, "command": command} for run, command in zip(preview.runs, (redact_command(command) for command in preview.commands), strict=True)],
    }


def input_image(image: InputImage) -> dict[str, Any]:
    return {"path": image.path, "bytes": image.bytes, "width": image.width, "height": image.height, "modified": image.modified}


def audit_entry(row: AuditRow) -> dict[str, Any]:
    return {"at": row.at, "action": row.action, "target": row.target, "outcome": row.outcome, "caller": row.caller}


def run_summary(run: RunRow) -> dict[str, Any]:
    return {
        "number": run.number,
        "status": run.status,
        "started_at": run.started_at,
        "seconds": run.seconds,
        "exit_code": run.exit_code,
        "output": run.output,
        "last_frame": run.last_frame,
        "output_width": run.output_width,
        "output_height": run.output_height,
        "output_frames": run.output_frames,
        "command": redact_command(run.command),
    }


def execution_summary(row: ExecutionRow) -> dict[str, Any]:
    return {
        "execution_id": row.label,
        "job_name": row.job_name,
        "job_file": row.job_file,
        "mode": row.mode,
        "status": row.status,
        "model": row.model,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "total_runs": row.total_runs,
        "succeeded": row.succeeded,
        "first_run": row.first_run,
        "resumes": execution_id_text(row.resumes) if row.resumes is not None else None,
    }


def execution_detail(row: ExecutionRow) -> dict[str, Any]:
    return {
        **execution_summary(row),
        "seed": row.seed,
        "seed_source": row.seed_source,
        "cooldown_seconds": row.cooldown_seconds,
        "cooldown_source": row.cooldown_source,
        "config_file": row.config_file,
        "runs": [run_summary(run) for run in row.runs],
    }


def execution_outputs(row: ExecutionRow) -> dict[str, Any]:
    """``GET /executions/{id}/outputs``: each run's output and last frame path, whether it exists, whether it is
    complete (only when its run succeeded), size, and measured dimensions; never the content."""
    return {"execution_id": row.label, "outputs": [_run_output(row, run) for run in row.runs]}


def _run_output(execution: ExecutionRow, run: RunRow) -> dict[str, Any]:
    output_path = execution.run_file(run.output)
    last_frame_path = execution.run_file(run.last_frame)
    output_exists = output_path is not None and output_path.exists()
    return {
        "number": run.number,
        "output": str(output_path) if output_path is not None else None,
        "output_exists": output_exists,
        "output_bytes": output_path.stat().st_size if output_exists and output_path is not None else None,
        "complete": run.status == "succeeded",
        "output_width": run.output_width,
        "output_height": run.output_height,
        "output_frames": run.output_frames,
        "last_frame": str(last_frame_path) if last_frame_path is not None else None,
        "last_frame_exists": last_frame_path is not None and last_frame_path.exists(),
    }


def queue_entry(row: QueueRow) -> dict[str, Any]:
    return {
        "queue_id": row.label,
        "job_path": row.job_path,
        "state": row.state,
        "submitted_at": row.submitted_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "execution_id": execution_id_text(row.execution_number) if row.execution_number is not None else None,
        # The queue entry this one resumes (Q0007), and the execution its resume point's last succeeded run came
        # from (E0012); two different numbers (state/schema.py's own comment on the queue table explains why).
        "resumes": queue_id_text(row.resumes) if row.resumes is not None else None,
        "resumes_execution": execution_id_text(row.resumes_execution) if row.resumes_execution is not None else None,
        "error": row.error,
    }
