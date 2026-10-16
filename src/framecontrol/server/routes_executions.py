"""``GET /v1/executions``, ``GET /v1/executions/{execution_id}``, ``GET /v1/executions/{execution_id}/outputs``,
``GET /v1/executions/{execution_id}/runs/{run}`` (with ``?stages=1`` its stage timing), ``GET /v1/executions/{execution_id}/runs/{run}/stages``, and ``POST /v1/executions/delete``, which writes one audit log entry per
execution it names."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from fastapi import APIRouter, Depends, Header

from framecontrol.core.clock import local_timestamp
from framecontrol.core.errors import FramecontrolError, InputError, NotFoundError
from framecontrol.server.caller import audit_caller
from framecontrol.server.context import ServerContext
from framecontrol.server.dependencies import Page, get_brief, get_context, get_marks, get_page, get_stages, require_auth
from framecontrol.server.job_reference import resolve_job_reference
from framecontrol.server.pagination import MAX_LIMIT, next_cursor
from framecontrol.server.serializers import delete_report, execution_detail, execution_outputs, execution_summary, run_summary, timing_detail
from framecontrol.state.execution_rows import ExecutionRow, RunRow
from framecontrol.state.ids import EXECUTION_LETTER, execution_id_text, parse_typed_id

router = APIRouter(dependencies=[Depends(require_auth)])

DELETE_ACTION = "delete_execution"


@dataclass
class DeleteBody:
    """Checked by the route, not by constraints here: a constraint would refuse the request before the route could
    record the refusal."""

    executions: list[str] = field(default_factory=list)
    dry_run: bool = False


@router.get("/v1/executions")
def list_executions(context: ServerContext = Depends(get_context), page: Page = Depends(get_page), status: str | None = None, job: str | None = None, name: str | None = None) -> dict[str, object]:
    # A job reference resolved to one file (as {job} is everywhere else), not the job's own name: (not an identifier).
    job_file = str(resolve_job_reference(context.catalog, job).resolve()) if job is not None else None
    # ``name`` matches the job name or file name as the TUI's /filter name does.
    rows = context.store.executions.page(limit=page.limit, offset=page.offset, status=status, job_file=job_file, name_contains=name or None)
    return {"executions": [execution_summary(row) for row in rows], "cursor": next_cursor(page.offset, page.limit, len(rows))}


@router.post("/v1/executions/delete")
def post_delete(body: DeleteBody, context: ServerContext = Depends(get_context), x_framecontrol_caller: str | None = Header(default=None, alias="X-Framecontrol-Caller")) -> dict[str, object]:
    """Delete one or several executions, or, with ``dry_run``, say what a deletion would do now. A
    refused or missing execution is not an error status: each one's outcome is in the answer, and has its own audit
    row. A request refused as a whole has one row, with no target; a dry run has none."""
    caller, caller_error = audit_caller(x_framecontrol_caller)
    try:
        if caller_error is not None:
            raise caller_error
        numbers = _delete_numbers(body.executions)
        # The submission lock first, as a resume takes it around the whole of resume_entry: a deletion cannot land
        # between its chain's resolution and its insert. The worker's own lock, inside, keeps a claim or a job start out.
        with context.submission_lock:
            report = context.worker.delete_executions(numbers, dry_run=body.dry_run)
    except FramecontrolError as error:
        if not body.dry_run:
            _audit(context, None, error.code, caller)
        raise
    except Exception:
        if not body.dry_run:
            _audit(context, None, "internal_error", caller)
        raise
    if not body.dry_run:
        outcomes = {**dict.fromkeys(report.deleted, "ok"), **dict.fromkeys(report.refused, "invalid_state"), **dict.fromkeys(report.missing, "not_found")}
        for number in numbers:
            _audit(context, execution_id_text(number), outcomes[number], caller)
    return delete_report(report, dry_run=body.dry_run)


def _delete_numbers(executions: list[str]) -> list[int]:
    """The execution numbers ``executions`` names, each once, in order; refuses none, more than ``MAX_LIMIT``, or an ID
    that does not parse."""
    if not 1 <= len(executions) <= MAX_LIMIT:
        raise InputError(f"'executions' must name 1 to {MAX_LIMIT} executions", field="executions")
    numbers: list[int] = []
    for text in executions:
        number = parse_typed_id(text, EXECUTION_LETTER)
        if number is None:
            raise InputError(f"'{text}' is not an execution ID; it should look like {execution_id_text(12)}", field="executions")
        numbers.append(number)
    return list(dict.fromkeys(numbers))


def _audit(context: ServerContext, target: str | None, outcome: str, caller: str) -> None:
    context.store.audit.record(action=DELETE_ACTION, target=target, outcome=outcome, caller=caller, at=local_timestamp(datetime.now()))


@router.get("/v1/executions/{execution_id}")
def get_execution(execution_id: str, context: ServerContext = Depends(get_context), brief: bool = Depends(get_brief)) -> dict[str, object]:
    """``?brief=1`` leaves out each run's command and gives each check as its stage and verdict: a long
    chain's full answer is past what an agent reads in one turn."""
    return execution_detail(_execution(context, execution_id), brief=brief)


@router.get("/v1/executions/{execution_id}/runs/{run}")
def get_execution_run(execution_id: str, run: str, context: ServerContext = Depends(get_context), stages: bool = Depends(get_stages), marks: bool = Depends(get_marks)) -> dict[str, object]:
    """One run, as the full answer gives it. ``run`` is its number in the chain, so a resumed
    execution's first is not 1. ``?stages=1`` adds its stage timing as ``timing``, with each stage's marks under ``?marks=1``."""
    row, found = _run(context, execution_id, run)
    summary = run_summary(found, backend=row.backend)
    if stages:
        summary["timing"] = _timing(context, row, found, marks)
    return summary


@router.get("/v1/executions/{execution_id}/runs/{run}/stages")
def get_execution_run_stages(execution_id: str, run: str, context: ServerContext = Depends(get_context), marks: bool = Depends(get_marks)) -> dict[str, object]:
    """What each stage of one run took: the child's stages as its output named them, and Framecontrol's own.
    ``?marks=1`` adds each stage's per-unit marks."""
    row, found = _run(context, execution_id, run)
    return {"execution_id": row.label, "run": found.number, **_timing(context, row, found, marks)}


@router.get("/v1/executions/{execution_id}/outputs")
def get_execution_outputs(execution_id: str, context: ServerContext = Depends(get_context)) -> dict[str, object]:
    return execution_outputs(_execution(context, execution_id))


def _run(context: ServerContext, execution_id: str, run: str) -> tuple[ExecutionRow, RunRow]:
    number = run.lstrip("0")
    if not (run.isascii() and run.isdigit() and number):
        raise InputError("'run' must be a positive integer", field="run")
    row = _execution(context, execution_id)
    # Compared as text: int() refuses a number of over 4300 digits, which no run has.
    found = next((candidate for candidate in row.runs if str(candidate.number) == number), None)
    if found is None:
        raise NotFoundError(f"{row.label} has no run {number}")
    return row, found


def _timing(context: ServerContext, row: ExecutionRow, found: RunRow, marks: bool) -> dict[str, object]:
    return timing_detail(context.store.timing.timing_of(row.id, found.number), context.store.timing.stages_of(row.id, found.number), marks=marks)


def _execution(context: ServerContext, execution_id: str) -> ExecutionRow:
    number = parse_typed_id(execution_id, EXECUTION_LETTER)
    row = context.store.executions.by_number(number) if number is not None else None
    if row is None:
        raise NotFoundError(f"No execution {execution_id}")
    return row
