"""``GET /v1/jobs``, ``GET /v1/jobs/{job}`` (and its brief view, ``?brief=1``), and ``GET /v1/jobs/{job}/preview``."""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends

from framecontrol.core.errors import InputError, NotFoundError
from framecontrol.core.yaml_files import read_bounded_bytes
from framecontrol.server.context import ServerContext
from framecontrol.server.dependencies import Page, get_brief, get_context, get_page, require_auth
from framecontrol.server.job_reference import listing_rows, resolve_job_reference
from framecontrol.server.pagination import next_cursor
from framecontrol.server.serializers import job_detail, job_preview, job_summary
from framecontrol.services.job_details import read_details

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/v1/jobs")
def list_jobs(context: ServerContext = Depends(get_context), page: Page = Depends(get_page)) -> dict[str, object]:
    rows = listing_rows(context.catalog.read())[page.offset : page.offset + page.limit]
    return {"jobs": [job_summary(row) for row in rows], "cursor": next_cursor(page.offset, page.limit, len(rows))}


@router.get("/v1/jobs/{job}")
def get_job(job: str, context: ServerContext = Depends(get_context), brief: bool = Depends(get_brief)) -> dict[str, object]:
    """``?brief=1`` leaves out a valid job's ``text``, which its resolved fields repeat; an invalid
    job keeps it, since that is what must be fixed. ``sha256`` stays either way."""
    path = resolve_job_reference(context.catalog, job)
    try:
        source = read_bounded_bytes(path, context.global_config.api_limits.max_job_file_bytes, "Job file")
        text = source.decode("utf-8", errors="replace")
    except OSError as error:
        raise NotFoundError(f"Cannot read {path.name}: {error.strerror}") from error
    details = read_details(path, context.global_config, context.paths, backends=context.executor.backends)
    if details.job is not None:
        detail = job_detail(details.job, text, sha256=hashlib.sha256(source).hexdigest())
        if brief:
            del detail["text"]
        return detail
    return {"text": text, "sha256": hashlib.sha256(source).hexdigest(), "error": details.error}


@router.get("/v1/jobs/{job}/preview")
def get_job_preview(job: str, context: ServerContext = Depends(get_context)) -> dict[str, object]:
    path = resolve_job_reference(context.catalog, job)
    details = read_details(path, context.global_config, context.paths, backends=context.executor.backends)
    if details.job is None:
        raise InputError(details.error or f"{path.name} is not a valid job", path=path)
    try:
        preview = context.executor.preview(details.job, executable=context.executable)
    except (ValueError, OSError) as error:
        raise InputError(str(error), path=path) from error
    return job_preview(details.job, preview)
