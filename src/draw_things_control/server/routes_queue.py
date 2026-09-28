"""``POST /v1/queue``, ``GET /v1/queue``, ``GET /v1/queue/{queue_id}``, ``POST /v1/queue/{queue_id}/cancel``, and
``POST /v1/queue/{queue_id}/resume``. Each writes one audit log entry, refusals included."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends

from draw_things_control.core.errors import NotFoundError
from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.jobs.parsing import load_job
from draw_things_control.server.audit import audited
from draw_things_control.server.context import ServerContext
from draw_things_control.server.dependencies import get_caller, get_context, require_auth
from draw_things_control.server.job_reference import resolve_job_reference
from draw_things_control.server.serializers import queue_entry
from draw_things_control.services.api_rules import check_api_rules
from draw_things_control.services.queue_cancel import cancel_entry
from draw_things_control.services.queue_resume import preview_resume, resume_entry
from draw_things_control.services.queue_submit import submit_job
from draw_things_control.state.ids import QUEUE_LETTER, parse_typed_id
from draw_things_control.state.queue import QueueRow, QueueState

router = APIRouter(dependencies=[Depends(require_auth)])


@dataclass
class SubmitBody:
    job: str


@router.post("/v1/queue")
def post_queue(body: SubmitBody, context: ServerContext = Depends(get_context), caller: str = Depends(get_caller)) -> dict[str, object]:
    with audited(context.store, action="submit", target=body.job, caller=caller):
        path = resolve_job_reference(context.catalog, body.job)
        job = load_job(path, context.global_config, context.paths.params)
        check_api_rules(job, context.global_config, context.global_config.api_limits, queued_count=_queued_count(context))
        entry = submit_job(path, context.global_config, context.paths.params, context.store)
        context.worker.wake()
    return queue_entry(entry)


@router.get("/v1/queue")
def get_queue(context: ServerContext = Depends(get_context), state: str | None = None) -> dict[str, object]:
    entries = context.store.queue.list(state=state)
    return {"queue": [queue_entry(row) for row in entries], "worker_state": context.worker.state(), "cooldown_until": context.worker.cooldown_until()}


@router.get("/v1/queue/{queue_id}")
def get_queue_entry(queue_id: str, context: ServerContext = Depends(get_context)) -> dict[str, object]:
    entry = _find_entry(context, queue_id)
    preview = preview_resume(context.store, entry, context.global_config, context.paths.params)
    is_current = context.worker.current_entry_id() == entry.id
    current_run = context.worker.current_run() if is_current else None
    return {
        **queue_entry(entry),
        "current_run": current_run[0] if current_run is not None else None,
        "current_run_elapsed_seconds": current_run[1] if current_run is not None else None,
        "last_run_seconds": _last_run_seconds(context, entry),
        "cooldown_until": context.worker.cooldown_until(),
        "resumable": preview.resumable,
        "resume_from_run": preview.from_run,
        "resume_refused_reason": preview.reason,
    }


def _last_run_seconds(context: ServerContext, entry: QueueRow) -> float | None:
    if entry.execution_number is None:
        return None
    execution = context.store.executions.by_number(entry.execution_number)
    return execution.runs[-1].seconds if execution is not None and execution.runs else None


@router.post("/v1/queue/{queue_id}/cancel")
def post_cancel(queue_id: str, context: ServerContext = Depends(get_context), caller: str = Depends(get_caller)) -> dict[str, object]:
    with audited(context.store, action="cancel", target=queue_id, caller=caller):
        entry = _find_entry(context, queue_id)
        cancel_entry(context.store, context.worker, entry.id)
        updated = _find_entry(context, queue_id)
    return queue_entry(updated)


@router.post("/v1/queue/{queue_id}/resume")
def post_resume(queue_id: str, context: ServerContext = Depends(get_context), caller: str = Depends(get_caller)) -> dict[str, object]:
    def before_submit(job: JobDefinition, remaining_runs: int) -> None:
        check_api_rules(job, context.global_config, context.global_config.api_limits, queued_count=_queued_count(context), remaining_runs=remaining_runs)

    with audited(context.store, action="resume", target=queue_id, caller=caller):
        entry = _find_entry(context, queue_id)
        resumed = resume_entry(context.store, entry.id, context.global_config, context.paths.params, before_submit=before_submit)
        context.worker.wake()
    return queue_entry(resumed)


def _queued_count(context: ServerContext) -> int:
    return len(context.store.queue.list(state=str(QueueState.QUEUED)))


def _find_entry(context: ServerContext, queue_id: str) -> QueueRow:
    number = parse_typed_id(queue_id, QUEUE_LETTER)
    if number is None:
        raise NotFoundError(f"No queue entry {queue_id}")
    entry = context.store.queue.by_number(number)
    if entry is None:
        raise NotFoundError(f"No queue entry {queue_id}")
    return entry
