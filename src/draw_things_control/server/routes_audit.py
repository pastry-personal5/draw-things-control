"""``GET /v1/audit``: the audit log, newest first."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from draw_things_control.server.context import ServerContext
from draw_things_control.server.dependencies import Page, get_context, get_page, require_auth
from draw_things_control.server.pagination import next_cursor
from draw_things_control.server.serializers import audit_entry

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/v1/audit")
def list_audit(context: ServerContext = Depends(get_context), page: Page = Depends(get_page)) -> dict[str, object]:
    rows = context.store.audit.page(limit=page.limit, offset=page.offset)
    return {"audit": [audit_entry(row) for row in rows], "cursor": next_cursor(page.offset, page.limit, len(rows))}
