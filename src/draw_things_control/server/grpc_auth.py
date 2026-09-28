"""Bearer-token authentication for the gRPC monitoring service: a server interceptor reading the ``authorization``
metadata key (``Bearer <token>``), the same token file and constant-time comparison as the HTTP API. Missing or
wrong, the call ends ``UNAUTHENTICATED``, never logged. Every RPC here is unary-request, streamed-response
(``WatchEvents``, ``WatchQueueEntry``), so rejection always builds a unary-stream handler."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import grpc
import grpc.aio

from draw_things_control.server.token_file import token_matches

AUTHORIZATION_KEY = "authorization"
BEARER_PREFIX = "Bearer "


class TokenAuthInterceptor(grpc.aio.ServerInterceptor):
    def __init__(self, token: str) -> None:
        self._token = token

    async def intercept_service(self, continuation: Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler | None]], handler_call_details: grpc.HandlerCallDetails) -> grpc.RpcMethodHandler | None:
        metadata = dict(handler_call_details.invocation_metadata or ())
        if token_matches(self._token, _bearer_token(metadata.get(AUTHORIZATION_KEY))):
            return await continuation(handler_call_details)
        return grpc.unary_stream_rpc_method_handler(_reject)


async def _reject(_request: object, context: grpc.aio.ServicerContext) -> AsyncIterator[object]:
    await context.abort(grpc.StatusCode.UNAUTHENTICATED, "")
    # abort() always raises; this satisfies the type checker that the generator has a body.
    yield  # pragma: no cover


def _bearer_token(value: str | bytes | None) -> str | None:
    # Binary metadata keys (ending "-bin") decode to bytes; "authorization" never does, but grpc's stub types it
    # as str | bytes for every key alike, so a non-str value is simply not a bearer token.
    return value.removeprefix(BEARER_PREFIX) if isinstance(value, str) and value.startswith(BEARER_PREFIX) else None
