"""Validation and optional write routes for API-managed job YAML files."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse

from draw_things_control.core.errors import InputError, LimitExceededError, NotFoundError
from draw_things_control.jobs.prompt_pairs import NAME_PATTERN
from draw_things_control.server.audit import audited
from draw_things_control.server.caller import audit_caller
from draw_things_control.server.context import ServerContext
from draw_things_control.server.dependencies import get_context, require_auth
from draw_things_control.server.serializers import job_detail
from draw_things_control.services.job_files_write import check_expected_sha256, create_job, delete_job, replace_job, sha256_bytes, validate_job_text

validation_router = APIRouter(dependencies=[Depends(require_auth)])
write_router = APIRouter(dependencies=[Depends(require_auth)])


async def _yaml_body(request: Request, context: ServerContext, *, overwrite: bool = False) -> tuple[str, str | None]:
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].lower() != "application/json":
        raise InputError("Content-Type must be application/json")
    limit = context.global_config.api_limits.max_job_file_bytes
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > 8 * limit:
        raise LimitExceededError(f"request body is over 8 times the max_job_file_bytes limit of {limit}", key="max_job_file_bytes", limit=8 * limit, value=int(length))
    raw = await request.body()
    if len(raw) > 8 * limit:
        raise LimitExceededError(f"request body is over 8 times the max_job_file_bytes limit of {limit}", key="max_job_file_bytes", limit=8 * limit, value=len(raw))
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InputError("Body must be a JSON object") from error
    allowed = {"yaml", "expected_sha256"} if overwrite else {"yaml"}
    if not isinstance(body, dict):
        raise InputError("Body must be a JSON object")
    unknown = set(body) - allowed
    if unknown:
        raise InputError(f"Unknown body key {sorted(unknown)[0]!r}", field=sorted(unknown)[0])
    if not isinstance(body.get("yaml"), str):
        raise InputError("'yaml' must be a string", field="yaml")
    expected = body.get("expected_sha256")
    if overwrite:
        expected = check_expected_sha256(expected)
    return body["yaml"], expected if isinstance(expected, str) else None


@validation_router.post("/v1/validate")
async def validate(request: Request, name: str | None = None, context: ServerContext = Depends(get_context)) -> dict[str, object]:
    text, _expected = await _yaml_body(request, context)
    job = validate_job_text(text, name=name, global_config=context.global_config, paths=context.paths)
    return job_detail(job, text, sha256=sha256_bytes(text.encode("utf-8")))


@write_router.put("/v1/jobs/{name}")
async def put_job(name: str, request: Request, overwrite: str | None = None, context: ServerContext = Depends(get_context), x_dtc_caller: str | None = Header(default=None, alias="X-Dtc-Caller")) -> JSONResponse:
    action = "replace_job" if overwrite == "1" else "create_job"
    caller, caller_error = audit_caller(x_dtc_caller)
    target = name if isinstance(name, str) and NAME_PATTERN.match(name) else None
    with audited(context.store, action=action, target=target, caller=caller):
        if caller_error is not None:
            raise caller_error
        if overwrite not in (None, "1"):
            raise InputError("'overwrite' must be 1", field="overwrite")
        text, expected = await _yaml_body(request, context, overwrite=overwrite == "1")
        with context.submission_lock:
            if overwrite == "1":
                target_path, _changed, job = replace_job(name, text, expected or "", global_config=context.global_config, paths=context.paths, store=context.store)
                status = 200
            else:
                if expected is not None:
                    raise InputError("'expected_sha256' is only valid with overwrite=1", field="expected_sha256")
                target_path, job = create_job(name, text, global_config=context.global_config, paths=context.paths)
                status = 201
            listing = context.catalog.read(fresh=True)
            row = next((row for row in listing.rows if row.path == target_path), None)
    return JSONResponse(status_code=status, content=job_detail(job, text, sha256=sha256_bytes(text.encode("utf-8")), job_id=row.job_id if row is not None else None))


@write_router.delete("/v1/jobs/{name}")
def remove_job(name: str, expected_sha256: str | None = None, context: ServerContext = Depends(get_context), x_dtc_caller: str | None = Header(default=None, alias="X-Dtc-Caller")) -> dict[str, object]:
    caller, caller_error = audit_caller(x_dtc_caller)
    with audited(context.store, action="delete_job", target=name if isinstance(name, str) and NAME_PATTERN.match(name) else None, caller=caller):
        if caller_error is not None:
            raise caller_error
        expected = check_expected_sha256(expected_sha256)
        with context.submission_lock:
            trash = delete_job(name, expected, paths=context.paths, store=context.store)
    try:
        relative = trash.relative_to(context.paths.root)
    except ValueError as error:
        raise NotFoundError(str(error)) from error
    return {"trash_path": str(relative)}
