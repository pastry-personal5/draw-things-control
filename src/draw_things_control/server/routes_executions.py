"""``GET /v1/executions``, ``GET /v1/executions/{execution_id}``, and ``GET /v1/executions/{execution_id}/outputs``."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from draw_things_control.core.errors import NotFoundError
from draw_things_control.server.context import ServerContext
from draw_things_control.server.dependencies import Page, get_context, get_page, require_auth
from draw_things_control.server.job_reference import resolve_job_reference
from draw_things_control.server.pagination import next_cursor
from draw_things_control.server.serializers import execution_detail, execution_outputs, execution_summary
from draw_things_control.state.ids import EXECUTION_LETTER, parse_typed_id

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/v1/executions")
def list_executions(context: ServerContext = Depends(get_context), page: Page = Depends(get_page), status: str | None = None, job: str | None = None) -> dict[str, object]:
    # A job reference resolved to one file (as {job} is everywhere else), not the job's own name: (not an identifier).
    job_file = str(resolve_job_reference(context.catalog, job).resolve()) if job is not None else None
    rows = context.store.executions.page(limit=page.limit, offset=page.offset, status=status, job_file=job_file)
    return {"executions": [execution_summary(row) for row in rows], "cursor": next_cursor(page.offset, page.limit, len(rows))}


@router.get("/v1/executions/{execution_id}")
def get_execution(execution_id: str, context: ServerContext = Depends(get_context)) -> dict[str, object]:
    row = context.store.executions.by_number(_execution_number(execution_id))
    if row is None:
        raise NotFoundError(f"No execution {execution_id}")
    return execution_detail(row)


@router.get("/v1/executions/{execution_id}/outputs")
def get_execution_outputs(execution_id: str, context: ServerContext = Depends(get_context)) -> dict[str, object]:
    row = context.store.executions.by_number(_execution_number(execution_id))
    if row is None:
        raise NotFoundError(f"No execution {execution_id}")
    return execution_outputs(row)


def _execution_number(execution_id: str) -> int:
    number = parse_typed_id(execution_id, EXECUTION_LETTER)
    if number is None:
        raise NotFoundError(f"No execution {execution_id}")
    return number
