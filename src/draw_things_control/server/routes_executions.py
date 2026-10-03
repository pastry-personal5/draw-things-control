"""``GET /v1/executions``, ``GET /v1/executions/{execution_id}``, ``GET /v1/executions/{execution_id}/outputs``,
from Milestone 06, ``POST /v1/executions/delete``, which writes one audit log entry per execution it names, and, from
Milestone 10, ``GET /v1/executions/{execution_id}/runs/{run}``."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from fastapi import APIRouter, Depends, Header

from draw_things_control.core.clock import local_timestamp
from draw_things_control.core.errors import DtcError, InputError, NotFoundError
from draw_things_control.server.caller import audit_caller
from draw_things_control.server.context import ServerContext
from draw_things_control.server.dependencies import Page, get_brief, get_context, get_page, require_auth
from draw_things_control.server.job_reference import resolve_job_reference
from draw_things_control.server.pagination import MAX_LIMIT, next_cursor
from draw_things_control.server.serializers import delete_report, execution_detail, execution_outputs, execution_summary, run_summary
from draw_things_control.state.execution_rows import ExecutionRow
from draw_things_control.state.ids import EXECUTION_LETTER, execution_id_text, parse_typed_id

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
    # ``name`` matches the job name or file name as the TUI's /filter name does (Milestone 06).
    rows = context.store.executions.page(limit=page.limit, offset=page.offset, status=status, job_file=job_file, name_contains=name or None)
    return {"executions": [execution_summary(row) for row in rows], "cursor": next_cursor(page.offset, page.limit, len(rows))}


@router.post("/v1/executions/delete")
def post_delete(body: DeleteBody, context: ServerContext = Depends(get_context), x_dtc_caller: str | None = Header(default=None, alias="X-Dtc-Caller")) -> dict[str, object]:
    """Delete one or several executions (Milestone 06), or, with ``dry_run``, say what a deletion would do now. A
    refused or missing execution is not an error status: each one's outcome is in the answer, and has its own audit
    row. A request refused as a whole has one row, with no target; a dry run has none."""
    caller, caller_error = audit_caller(x_dtc_caller)
    try:
        if caller_error is not None:
            raise caller_error
        numbers = _delete_numbers(body.executions)
        # The submission lock first, as a resume takes it around the whole of resume_entry: a deletion cannot land
        # between its chain's resolution and its insert. The worker's own lock, inside, keeps a claim or a job start out.
        with context.submission_lock:
            report = context.worker.delete_executions(numbers, dry_run=body.dry_run)
    except DtcError as error:
        if not body.dry_run:
            _audit(context, None, error.code, caller)
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
    """``?brief=1`` (Milestone 10) leaves out each run's command and gives each check as its stage and verdict: a long
    chain's full answer is past what an agent reads in one turn."""
    return execution_detail(_execution(context, execution_id), brief=brief)


@router.get("/v1/executions/{execution_id}/runs/{run}")
def get_execution_run(execution_id: str, run: str, context: ServerContext = Depends(get_context)) -> dict[str, object]:
    """One run, as the full answer gives it (Milestone 10). ``run`` is its number in the chain, so a resumed
    execution's first is not 1."""
    number = run.lstrip("0")
    if not (run.isascii() and run.isdigit() and number):
        raise InputError("'run' must be a positive integer", field="run")
    row = _execution(context, execution_id)
    # Compared as text: int() refuses a number of over 4300 digits, which no run has.
    found = next((candidate for candidate in row.runs if str(candidate.number) == number), None)
    if found is None:
        raise NotFoundError(f"{row.label} has no run {number}")
    return run_summary(found)


@router.get("/v1/executions/{execution_id}/outputs")
def get_execution_outputs(execution_id: str, context: ServerContext = Depends(get_context)) -> dict[str, object]:
    return execution_outputs(_execution(context, execution_id))


def _execution(context: ServerContext, execution_id: str) -> ExecutionRow:
    number = parse_typed_id(execution_id, EXECUTION_LETTER)
    row = context.store.executions.by_number(number) if number is not None else None
    if row is None:
        raise NotFoundError(f"No execution {execution_id}")
    return row
