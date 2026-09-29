"""``POST /v1/queue``, ``GET /v1/queue``, ``GET /v1/queue/{queue_id}``, ``POST /v1/queue/{queue_id}/cancel``,
``POST /v1/queue/{queue_id}/resume``, and, from Milestone 05, ``POST /v1/queue/{queue_id}/park`` and ``/unpark``, and
``POST /v1/queue/hold`` and ``/release``. Each POST writes one audit log entry, refusals included."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Header

from draw_things_control.core.errors import NotFoundError
from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.server.audit import audited
from draw_things_control.server.caller import audit_caller
from draw_things_control.server.context import ServerContext
from draw_things_control.server.dependencies import Page, get_context, get_page, require_auth
from draw_things_control.server.job_reference import resolve_job_reference
from draw_things_control.server.pagination import next_cursor
from draw_things_control.server.serializers import queue_entry, queue_hold
from draw_things_control.services.api_rules import check_api_rules
from draw_things_control.services.queue_cancel import cancel_entry
from draw_things_control.services.queue_park import park_entry, unpark_entry
from draw_things_control.services.queue_resume import preview_resume, resume_entry
from draw_things_control.services.queue_submit import submit_job
from draw_things_control.state.ids import QUEUE_LETTER, parse_typed_id
from draw_things_control.state.queue import FINISHED_STATES, QueueRow, QueueState

router = APIRouter(dependencies=[Depends(require_auth)])

# String values, for a plain membership check against the ?state= query parameter, which is not validated against
# QueueState (an unknown value has always just matched nothing, in list() as in list_finished() below).
_FINISHED_STATE_VALUES = frozenset(str(state) for state in FINISHED_STATES)


@dataclass
class SubmitBody:
    job: str


@router.post("/v1/queue")
def post_queue(body: SubmitBody, context: ServerContext = Depends(get_context), x_dtc_caller: str | None = Header(default=None, alias="X-Dtc-Caller")) -> dict[str, object]:
    def before_submit(job: JobDefinition) -> None:
        check_api_rules(job, context.global_config, context.global_config.api_limits, queued_count=_queued_count(context))

    caller, caller_error = audit_caller(x_dtc_caller)
    with audited(context.store, action="submit", target=body.job, caller=caller):
        if caller_error is not None:
            raise caller_error
        path = resolve_job_reference(context.catalog, body.job)
        # Held across the check and the insert: two concurrent submissions could otherwise both read the queued
        # count before either inserts, both pass check_api_rules, and together push the queue past max_queued_jobs.
        # The rules run inside submit_job, on the job parsed from the exact text it stores, so a file edited
        # between an earlier read and the snapshot cannot slip past them. The insert itself, and the 'queued' event
        # it publishes, go through the worker's own enqueue: it runs both under the same lock the worker's claim
        # loop takes, so a client watching events can never see this entry's 'running' before its own 'queued'.
        with context.submission_lock:
            entry = submit_job(path, context.global_config, context.paths.params, context.store, before_submit=before_submit, enqueue=context.worker.enqueue)
    return queue_entry(entry)


@router.get("/v1/queue")
def get_queue(context: ServerContext = Depends(get_context), page: Page = Depends(get_page), state: str | None = None) -> dict[str, object]:
    """Queued and running entries always come back in full (bounded by ``max_queued_jobs``, and only one entry is
    ever running); ``?state=`` naming one of them behaves exactly as before, an unpaged filter. Finished entries are
    the queue's own history, unbounded once ``history_retention_days: 0``, so they page like ``GET /jobs`` and
    ``/executions``: newest first, and, on the first page only (``cursor`` absent), after the active entries above."""
    if state is not None and state not in _FINISHED_STATE_VALUES:
        # Joined, like list_active's own unfiltered read below and by_number: an active entry's succeeded must
        # agree with the other two reads (queue.list(state=...) is unjoined, and would always report 0 here).
        entries = context.store.queue.list_active(state=state)
        cursor = None
    else:
        finished = context.store.queue.list_finished(limit=page.limit, offset=page.offset, state=state)
        active = context.store.queue.list_active() if state is None and page.offset == 0 else []
        entries = active + finished
        cursor = next_cursor(page.offset, page.limit, len(finished))
    parking = context.worker.parking_entry_id()
    return {"queue": [queue_entry(row, park_requested=row.id == parking) for row in entries], "worker_state": context.worker.state(), "cooldown_until": context.worker.cooldown_until(), **queue_hold(context.worker.hold_state()), "cursor": cursor}


@router.get("/v1/queue/{queue_id}")
def get_queue_entry(queue_id: str, context: ServerContext = Depends(get_context)) -> dict[str, object]:
    return _entry_detail(context, _find_entry(context, queue_id))


def _entry(context: ServerContext, entry: QueueRow) -> dict[str, object]:
    """An entry, with the worker's park reservation, which is kept in memory, not in the row."""
    return queue_entry(entry, park_requested=context.worker.park_requested(entry.id))


def _entry_detail(context: ServerContext, entry: QueueRow) -> dict[str, object]:
    """``GET /v1/queue/{queue_id}``'s entry, as park and unpark also return it: its current run, and the run it is
    between runs after (``between_runs_after_run``, a cooldown between them included: ``cooldown_until`` is the wait
    between two queued jobs, None while a job runs), let a front end word a park's outcome ("parks after run 3/7"),
    and the hold its effect."""
    preview = preview_resume(context.store, entry, context.global_config, context.paths.params)
    is_current = context.worker.current_entry_id() == entry.id
    current_run = context.worker.current_run() if is_current else None
    current_step = context.worker.current_step() if is_current else None
    return {
        **_entry(context, entry),
        "current_run": current_run[0] if current_run is not None else None,
        "current_run_elapsed_seconds": current_run[1] if current_run is not None else None,
        "current_step": current_step[0] if current_step is not None else None,
        "current_step_total": current_step[1] if current_step is not None else None,
        "between_runs_after_run": context.worker.between_runs_after() if is_current else None,
        "last_run_seconds": _last_run_seconds(context, entry),
        "cooldown_until": context.worker.cooldown_until(),
        **queue_hold(context.worker.hold_state()),
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
def post_cancel(queue_id: str, context: ServerContext = Depends(get_context), x_dtc_caller: str | None = Header(default=None, alias="X-Dtc-Caller")) -> dict[str, object]:
    caller, caller_error = audit_caller(x_dtc_caller)
    with audited(context.store, action="cancel", target=queue_id, caller=caller):
        if caller_error is not None:
            raise caller_error
        entry = _find_entry(context, queue_id)
        # cancel_entry publishes the change itself either way: through worker.cancel_queued for a queued entry, or
        # the worker's own finish (from cancel_running stopping it) for a running one.
        cancel_entry(context.store, context.worker, entry.id)
        updated = _find_entry(context, queue_id)
    return _entry(context, updated)


@router.post("/v1/queue/hold")
def post_hold(context: ServerContext = Depends(get_context), x_dtc_caller: str | None = Header(default=None, alias="X-Dtc-Caller")) -> dict[str, object]:
    """Hold the queue (Milestone 05): a running job is not stopped, and nothing starts after it until a release.
    ``changed`` is False when the queue was already held."""
    caller, caller_error = audit_caller(x_dtc_caller)
    with audited(context.store, action="hold", target=None, caller=caller):
        if caller_error is not None:
            raise caller_error
        changed, hold = context.worker.hold()
    return {**queue_hold(hold), "changed": changed}


@router.post("/v1/queue/release")
def post_release(context: ServerContext = Depends(get_context), x_dtc_caller: str | None = Header(default=None, alias="X-Dtc-Caller")) -> dict[str, object]:
    """End the hold: the oldest queued entry starts at once. ``changed`` is False when the queue was not held."""
    caller, caller_error = audit_caller(x_dtc_caller)
    with audited(context.store, action="release", target=None, caller=caller):
        if caller_error is not None:
            raise caller_error
        changed, hold = context.worker.release()
    return {**queue_hold(hold), "changed": changed}


@router.post("/v1/queue/{queue_id}/park")
def post_park(queue_id: str, context: ServerContext = Depends(get_context), x_dtc_caller: str | None = Header(default=None, alias="X-Dtc-Caller")) -> dict[str, object]:
    """Park a running entry: it ends once its current run finishes, and the queue is held (Milestone 05)."""
    caller, caller_error = audit_caller(x_dtc_caller)
    with audited(context.store, action="park", target=queue_id, caller=caller):
        if caller_error is not None:
            raise caller_error
        entry = _find_entry(context, queue_id)
        park_entry(context.store, context.worker, entry.id)
        updated = _find_entry(context, queue_id)
    return _entry_detail(context, updated)


@router.post("/v1/queue/{queue_id}/unpark")
def post_unpark(queue_id: str, context: ServerContext = Depends(get_context), x_dtc_caller: str | None = Header(default=None, alias="X-Dtc-Caller")) -> dict[str, object]:
    """Withdraw a running entry's park reservation; the job runs on, and the hold is released when that reservation made it."""
    caller, caller_error = audit_caller(x_dtc_caller)
    with audited(context.store, action="unpark", target=queue_id, caller=caller):
        if caller_error is not None:
            raise caller_error
        entry = _find_entry(context, queue_id)
        unpark_entry(context.store, context.worker, entry.id)
        updated = _find_entry(context, queue_id)
    return _entry_detail(context, updated)


@router.post("/v1/queue/{queue_id}/resume")
def post_resume(queue_id: str, context: ServerContext = Depends(get_context), x_dtc_caller: str | None = Header(default=None, alias="X-Dtc-Caller")) -> dict[str, object]:
    def before_submit(job: JobDefinition, remaining_runs: int) -> None:
        check_api_rules(job, context.global_config, context.global_config.api_limits, queued_count=_queued_count(context), remaining_runs=remaining_runs)

    caller, caller_error = audit_caller(x_dtc_caller)
    with audited(context.store, action="resume", target=queue_id, caller=caller):
        if caller_error is not None:
            raise caller_error
        entry = _find_entry(context, queue_id)
        # Held across before_submit's check_api_rules call and resume_entry's own insert, for the same reason
        # post_queue holds it: closes the same race on max_queued_jobs for a resume. The insert and its 'queued'
        # event go through the worker's own enqueue, for the same event-ordering reason post_queue uses it.
        with context.submission_lock:
            resumed = resume_entry(context.store, entry.id, context.global_config, context.paths.params, before_submit=before_submit, enqueue=context.worker.enqueue)
    return queue_entry(resumed)


def _queued_count(context: ServerContext) -> int:
    return context.store.queue.count(state=str(QueueState.QUEUED))


def _find_entry(context: ServerContext, queue_id: str) -> QueueRow:
    number = parse_typed_id(queue_id, QUEUE_LETTER)
    if number is None:
        raise NotFoundError(f"No queue entry {queue_id}")
    entry = context.store.queue.by_number(number)
    if entry is None:
        raise NotFoundError(f"No queue entry {queue_id}")
    return entry
