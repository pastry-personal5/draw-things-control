"""``GET /v1/jobs``, ``GET /v1/jobs/{job}``, and ``GET /v1/jobs/{job}/preview``."""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends

from draw_things_control.core.errors import InputError, NotFoundError
from draw_things_control.server.context import ServerContext
from draw_things_control.server.dependencies import Page, get_context, get_page, require_auth
from draw_things_control.server.job_reference import listing_rows, resolve_job_reference
from draw_things_control.server.pagination import next_cursor
from draw_things_control.server.serializers import job_detail, job_preview, job_summary
from draw_things_control.services.job_details import read_details

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/v1/jobs")
def list_jobs(context: ServerContext = Depends(get_context), page: Page = Depends(get_page)) -> dict[str, object]:
    rows = listing_rows(context.catalog.read())[page.offset : page.offset + page.limit]
    return {"jobs": [job_summary(row) for row in rows], "cursor": next_cursor(page.offset, page.limit, len(rows))}


@router.get("/v1/jobs/{job}")
def get_job(job: str, context: ServerContext = Depends(get_context)) -> dict[str, object]:
    path = resolve_job_reference(context.catalog, job)
    try:
        source = path.read_bytes()
        text = source.decode("utf-8")
    except OSError as error:
        raise NotFoundError(f"Cannot read {path.name}: {error.strerror}") from error
    details = read_details(path, context.global_config, context.paths)
    if details.job is not None:
        return job_detail(details.job, text, sha256=hashlib.sha256(source).hexdigest())
    return {"text": text, "sha256": hashlib.sha256(source).hexdigest(), "error": details.error}


@router.get("/v1/jobs/{job}/preview")
def get_job_preview(job: str, context: ServerContext = Depends(get_context)) -> dict[str, object]:
    path = resolve_job_reference(context.catalog, job)
    details = read_details(path, context.global_config, context.paths)
    if details.job is None:
        raise InputError(details.error or f"{path.name} is not a valid job", path=path)
    try:
        preview = context.executor.preview(details.job, executable=context.executable)
    except (ValueError, OSError) as error:
        raise InputError(str(error), path=path) from error
    return job_preview(details.job, preview)
