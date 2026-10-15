"""Turns the package's own dataclasses into the API's JSON-safe response bodies. Commands were redacted when
stored, and are redacted again as they are served (``redact_command``), so an older row cannot leak a credential."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from framecontrol.core.arguments import redact_command
from framecontrol.jobs.definition import JobDefinition, PromptPair
from framecontrol.jobs.planning import JobPreview
from framecontrol.services.history_delete import DeleteReport
from framecontrol.services.input_listing import InputImage
from framecontrol.services.job_catalog import JobRow
from framecontrol.services.queue_hold import HoldState
from framecontrol.state.audit import AuditRow
from framecontrol.state.execution_rows import ExecutionRow, MediaCheckRow, RunRow
from framecontrol.state.ids import execution_id_text, queue_id_text
from framecontrol.state.queue import QueueRow


def job_summary(row: JobRow) -> dict[str, Any]:
    """One row of ``GET /jobs``: job ID, file name, job name, mode, runs, and validity or the first error and field."""
    base = {"job_id": row.job_id, "file_name": row.path.name}
    if row.job is not None:
        return {**base, "name": row.job.name, "mode": row.job.mode_label, "backend": row.job.backend, "runs": row.job.run_count, "valid": True, "error": None, "field": None}
    return {**base, "name": None, "mode": None, "backend": None, "runs": None, "valid": False, "error": row.error, "field": row.error_field}


def prompt_pair(pair: PromptPair, runs: tuple[int, ...] | None = None) -> dict[str, Any]:
    return {"name": pair.name, "positive": pair.positive, "negative": pair.negative, "runs": list(pair.runs if runs is None else runs), "default": pair.default}


def job_detail(job: JobDefinition, source_text: str, *, sha256: str | None = None, job_id: str | None = None) -> dict[str, Any]:
    """``GET /jobs/{job}``: the file's text, and the resolved job (prompt pairs, size, seed, cooldown and their
    sources)."""
    seed, seed_source = job.configured_seed()
    detail = {
        "text": source_text,
        "name": job.name,
        "mode": job.mode_label,
        "backend": job.backend,
        "backend_settings": job.backend_settings,
        "input": str(job.input) if job.input is not None else None,
        "run_count": job.run_count,
        "prompt_pairs": [prompt_pair(pair, tuple(run.number for run in job.effective_runs if run.prompt_pair == pair.name) if job.effective_runs else None) for pair in job.prompt_pairs],
        "output_directory": str(job.output_directory),
        "extension": job.extension,
        "video_format": job.video_format,
        "config_file": job.config_file,
        "config_override": job.config_override.as_dict(),
        "model": job.model,
        "run_timeout_seconds": job.run_timeout_seconds,
        "size": list(job.size) if job.size is not None else None,
        "seed": seed,
        "seed_source": seed_source,
        "cooldown": job.cooldown.as_dict(),
        "cooldown_source": job.cooldown_source,
        # The color correction: {"anchor": "none"} for a job without one; null for a version-2 job, whose
        # runs each have their own, in runs[].settings.
        "color": None if job.effective_runs else job.color.as_dict(),
    }
    if job.effective_runs:
        # A version-2 job is its compiled runs: each run's backend, pipeline, catalog selections, and resolved knobs.
        detail["format_version"] = job.version
        detail["runs"] = list(job.effective_plan())
    if sha256 is not None:
        detail["sha256"] = sha256
    if job_id is not None:
        detail["job_id"] = job_id
    return detail


def _run_plan(job: JobDefinition, number: int) -> dict[str, Any]:
    """A version-2 run's compiled identity and knobs, which a version-1 job's runs do not have."""
    run = job.effective_run(number)
    if run is None:
        return {}
    return {"backend": run.backend, "pipeline": run.pipeline, "prompt_pair": run.prompt_pair, "settings_profile": run.settings_profile, "knobs": dict(run.knobs), "knob_sources": dict(run.knob_sources)}


def job_preview(job: JobDefinition, preview: JobPreview) -> dict[str, Any]:
    """``GET /jobs/{job}/preview``: the dry-run plan, commands redacted."""
    return {
        "backend": job.backend,
        "seed": preview.seed,
        "seed_source": preview.seed_source,
        "runs": [{"number": run.number, **_run_plan(job, run.number), "pair": run.pair.name, "input": str(run.input) if run.input is not None else None, "output": str(run.output), "last_frame": str(run.last_frame) if run.last_frame is not None else None, "command": command} for run, command in zip(preview.runs, (redact_command(command) for command in preview.commands), strict=True)],
    }


def input_image(image: InputImage) -> dict[str, Any]:
    return {"path": image.path, "bytes": image.bytes, "width": image.width, "height": image.height, "modified": image.modified}


def audit_entry(row: AuditRow) -> dict[str, Any]:
    return {"at": row.at, "action": row.action, "target": row.target, "outcome": row.outcome, "caller": row.caller}


def run_summary(run: RunRow, *, brief: bool = False, backend: str = "draw-things-cli") -> dict[str, Any]:
    """One run, as ``GET /executions/{id}`` and ``GET /executions/{id}/runs/{run}`` give it; ``brief``
    leaves out its command and gives each check as its stage and verdict alone."""
    return {
        "number": run.number,
        "pair": run.pair,
        **({"backend": run.backend or backend, "pipeline": run.pipeline, "settings_profile": run.settings_profile, "knobs": run.knobs, "knob_sources": run.knob_sources, "input_run": run.input_run, "input_role": run.input_role} if run.backend is not None else {}),
        "status": run.status,
        "started_at": run.started_at,
        "seconds": run.seconds,
        "exit_code": run.exit_code,
        "output": run.output,
        "last_frame": run.last_frame,
        "output_width": run.output_width,
        "output_height": run.output_height,
        "output_frames": run.output_frames,
        "output_frame_rate": run.output_frame_rate,
        "output_duration": run.output_duration,
        "audio_codec": run.audio_codec,
        "audio_sample_rate": run.audio_sample_rate,
        "audio_channels": run.audio_channels,
        "audio_duration": run.audio_duration,
        # The corrected copy's file name, and the file the run's colors were held to.
        "corrected_output": run.corrected_output,
        "anchor": Path(run.anchor).name if backend == "h3" and run.anchor is not None else run.anchor,
        **({} if brief else {"command": redact_command(run.command)}),
        "checks": [brief_check(check) if brief else media_check(check) for check in run.checks],
    }


def media_check(check: MediaCheckRow) -> dict[str, Any]:
    return {"stage": check.stage, "file": check.file, "summary": check.summary, "verdict": check.verdict, "notes": list(check.notes), "facts": check.facts, "at": check.at}


def brief_check(check: MediaCheckRow) -> dict[str, Any]:
    """A check in an execution's brief view: its stage and verdict, without its facts and notes."""
    return {"stage": check.stage, "verdict": check.verdict}


def unstarted_check(check: MediaCheckRow, *, brief: bool = False) -> dict[str, Any]:
    """A check of a run that never started, listed on the execution, so it names the run it was made before."""
    return {"run": check.run, **(brief_check(check) if brief else media_check(check))}


def execution_summary(row: ExecutionRow) -> dict[str, Any]:
    return {
        "execution_id": row.label,
        "source_kind": row.source_kind,
        "backend": row.backend,
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


def execution_detail(row: ExecutionRow, *, brief: bool = False) -> dict[str, Any]:
    """``GET /executions/{id}``; ``brief`` (``?brief=1``) gives each run and check as ``run_summary`` and
    ``unstarted_check`` do, so a long chain's answer fits what an agent reads."""
    return {
        **execution_summary(row),
        "seed": row.seed,
        "seed_source": row.seed_source,
        "cooldown_seconds": row.cooldown_seconds,
        "cooldown_source": row.cooldown_source,
        # The resolved cooldown mapping (mode, ratio, minimum/maximum_seconds), not only its old fixed-seconds
        # summary above: None for an execution written before it was kept (ExecutionSettings' own comment), which
        # had only cooldown_seconds. The TUI's seeding (attaching to a running entry with no live
        # JobStarted to read a CooldownPolicy from) is the first reader of it over the API.
        "cooldown": row.settings.cooldown,
        "backend_settings": row.settings.backend_settings or {},
        "config_file": row.config_file,
        "manifest": row.manifest_path,
        "log": row.log_path,
        # The chain's first image, which its color checks compare with; None before it was kept.
        "first_image": row.first_image,
        "runs": [run_summary(run, brief=brief, backend=row.backend) for run in row.runs],
        # Checks made before a run that never started (a stop during the input checks); each names its run.
        "checks": [unstarted_check(check, brief=brief) for check in row.checks],
    }


def execution_outputs(row: ExecutionRow) -> dict[str, Any]:
    """``GET /executions/{id}/outputs``: each run's output and last frame path, whether it exists, whether it is
    complete (only when its run succeeded), size, and measured dimensions; never the content."""
    return {"execution_id": row.label, "outputs": [_run_output(row, run) for run in row.runs]}


def _run_output(execution: ExecutionRow, run: RunRow) -> dict[str, Any]:
    output_path = execution.run_file(run.output)
    last_frame_path = execution.run_file(run.last_frame)
    corrected_path = execution.run_file(run.corrected_output)
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
        "output_frame_rate": run.output_frame_rate,
        "output_duration": run.output_duration,
        "audio_codec": run.audio_codec,
        "audio_sample_rate": run.audio_sample_rate,
        "audio_channels": run.audio_channels,
        "audio_duration": run.audio_duration,
        "last_frame": str(last_frame_path) if last_frame_path is not None else None,
        "last_frame_exists": last_frame_path is not None and last_frame_path.exists(),
        "corrected_output": str(corrected_path) if corrected_path is not None else None,
        "corrected_output_exists": corrected_path is not None and corrected_path.exists(),
    }


def queue_entry(row: QueueRow, *, park_requested: bool = False) -> dict[str, Any]:
    """``park_requested`` is the worker's, since a park reservation is kept in memory, not in the row."""
    generation = None
    if row.kind == "generate":
        from framecontrol.services.generation_submit import GenerationSnapshot

        snapshot = GenerationSnapshot.from_json(row.snapshot)
        generation = {"model": snapshot.model, "output": snapshot.output}
    return {
        "queue_id": row.label,
        "kind": row.kind,
        "backend": row.backend,
        "job_path": row.job_path if row.kind == "job" else None,
        "generation": generation,
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
        "total_runs": row.total_runs,
        "succeeded": row.succeeded,
        "park_requested": park_requested,
        # The caller that submitted or resumed it; None for an entry made before it was recorded.
        "submitted_by": row.submitted_by,
    }


def queue_hold(hold: HoldState) -> dict[str, Any]:
    """The queue's hold, beside the entries of ``GET /queue`` and ``GET /queue/{id}``, and the response
    of ``POST /queue/hold`` and ``/queue/release``. ``hold_caller`` is the caller that made it."""
    return {"held": hold.held, "held_since": hold.since, "held_by": hold.by, "hold_caller": hold.caller}


def delete_report(report: DeleteReport, *, dry_run: bool) -> dict[str, Any]:
    """``POST /executions/delete``'s answer: each execution's outcome, the entries each deletion leaves
    unresumable, and the manifests that could not be deleted."""
    return {
        "dry_run": dry_run,
        "deleted": [execution_id_text(number) for number in report.deleted],
        "refused": [{"execution_id": execution_id_text(number), "reason": reason} for number, reason in report.refused.items()],
        "missing": [execution_id_text(number) for number in report.missing],
        "resumes_ended": [{"execution_id": execution_id_text(number), "queue_ids": queue_ids} for number, queue_ids in report.resumes_ended.items()],
        "manifests_kept": [{"execution_id": execution_id_text(number), "path": path} for number, path in report.manifests_kept],
    }
