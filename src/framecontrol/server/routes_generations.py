"""The person-facing one-off generation endpoints."""

from __future__ import annotations

import shlex
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header

from framecontrol.core.arguments import redact_command
from framecontrol.core.clock import local_timestamp
from framecontrol.server.audit import audited
from framecontrol.server.caller import audit_caller
from framecontrol.server.context import ServerContext
from framecontrol.server.dependencies import get_context, require_auth
from framecontrol.server.serializers import queue_entry
from framecontrol.services.api_rules import check_queue_not_full
from framecontrol.services.generation_submit import GenerationSnapshot, parse_generation
from framecontrol.state.executions import ExecutionSettings
from framecontrol.state.queue import NewQueueEntry, QueueRow

router = APIRouter(dependencies=[Depends(require_auth)])


@router.post("/v1/generations/preview")
def preview_generation(body: dict[str, Any], context: ServerContext = Depends(get_context)) -> dict[str, object]:
    """Validate exactly what a submit would run, but do not write, queue, or audit it."""
    snapshot = parse_generation(body, context.global_config, context.paths.params)
    return {"command": shlex.join(redact_command(snapshot.arguments(context.executable).command))}


@router.post("/v1/generations")
def submit_generation(body: dict[str, Any], context: ServerContext = Depends(get_context), x_framecontrol_caller: str | None = Header(default=None, alias="X-Framecontrol-Caller")) -> dict[str, object]:
    caller, caller_error = audit_caller(x_framecontrol_caller)
    with audited(context.store, action="generate", target=None, caller=caller) as audit:
        if caller_error is not None:
            raise caller_error
        snapshot = parse_generation(body, context.global_config, context.paths.params)
        audit.value = snapshot.output
        with context.submission_lock:
            check_queue_not_full(context.store.queue.count(state="queued"), context.global_config.api_limits)
            entry = context.worker.enqueue(lambda: _submit(context, snapshot, caller))
    return queue_entry(entry)


def _submit(context: ServerContext, snapshot: GenerationSnapshot, caller: str) -> QueueRow:
    return context.store.queue.submit(NewQueueEntry(job_path="", job_text="", config_file="", config_text="", input_directory=str(context.global_config.input_directory), output_directory=str(context.global_config.output_directory), cooldown_default=None, settings=ExecutionSettings(input=snapshot.image[0] if snapshot.image else None, output_directory=str(context.global_config.output_directory), config_file=snapshot.config_file), submitted_at=local_timestamp(datetime.now()), total_runs=1, submitted_by=caller, kind="generate", snapshot=snapshot.to_json()))
