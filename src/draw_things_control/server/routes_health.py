"""``GET /health`` (no auth: liveness) and ``GET /capabilities`` (whether writes are enabled, and the limits in
force, so an agent can plan)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from draw_things_control.server.context import ServerContext, server_version
from draw_things_control.server.dependencies import get_context, require_auth

health_router = APIRouter()
capabilities_router = APIRouter(dependencies=[Depends(require_auth)])


@health_router.get("/v1/health")
def health(context: ServerContext = Depends(get_context)) -> dict[str, object]:
    """The server is up, its version, whether the worker is alive, and the gRPC port sharing its own host (Milestone
    03: every gRPC client derives its target from this and the HTTP host it was already given, no flag of its own).
    Always 200 while this process answers at all, so a caller must read ``worker_alive``, not just the status."""
    return {"status": "ok", "version": server_version(), "worker_alive": context.worker.is_alive(), "grpc_port": context.grpc_port}


@capabilities_router.get("/v1/capabilities")
def capabilities(context: ServerContext = Depends(get_context)) -> dict[str, object]:
    limits = context.global_config.api_limits
    return {
        "allow_write": context.allow_write,
        "limits": {
            "max_queued_jobs": limits.max_queued_jobs,
            "max_job_runs": limits.max_job_runs,
            "max_job_seconds": limits.max_job_seconds,
            "max_job_file_bytes": limits.max_job_file_bytes,
        },
    }
