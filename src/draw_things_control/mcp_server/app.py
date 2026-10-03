"""``dtc mcp`` (Milestone 10): an MCP server on stdio, or, from Milestone 13, on Streamable HTTP (``http.py``), whose tools and resources call ``dtc serve``'s HTTP API and
nothing else. Built on the SDK's low-level ``Server``, not ``MCPServer``: the tools need input schemas written with the
API's names, a list that changes at runtime, and error results that carry the API's error shape as structured
content. ``build_server`` is what the tests call; ``run`` is what ``cli/app.py`` calls."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import anyio
import httpx
import mcp_types as types
from loguru import logger
from mcp.server.caching import CacheHint
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler, ToolsListChanged
from mcp.shared.exceptions import MCPError
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from draw_things_control.mcp_server.api import ApiClient, ToolError, path_segment
from draw_things_control.mcp_server.http import BindError as BindError  # re-exported: cli/app.py may import this module alone
from draw_things_control.mcp_server.http import serve_http
from draw_things_control.mcp_server.tools import TOOLS, Tool, check_arguments, listed_tools
from draw_things_control.mcp_server.watch import WaitTimes, wait_for_change

NAME = "dtc"
# Claude Code keeps these, and the tool names, in context on every turn, and loads a tool's schema only when it is
# used: a few lines, with what each tool must say left to its description.
INSTRUCTIONS = "Drives dtc serve, which runs long image-to-video chains one at a time on this machine. For a job: get_capabilities and list_inputs; validate_job_text a draft; create_job (listed only while dtc serve runs with --allow-write); submit_job; get_queue_entry with a long wait_seconds to follow it, about one call a run; list_outputs. People share the queue: act only on entries you submitted."
# How long a read of GET /v1/capabilities stands before a tool call reads it again; a tools/list answer carries the
# same as its cache hint, so a client that does not listen for changes keeps a stale list no longer.
CAPABILITIES_FRESH_SECONDS = 5.0
# A capabilities read before a call waits no longer than this, so a slow API cannot double a call's time.
CAPABILITIES_TIMEOUT_SECONDS = 5.0
LIST_CACHE_HINT = CacheHint(ttl_ms=int(CAPABILITIES_FRESH_SECONDS * 1000))
CAPABILITIES_PATH = "/v1/capabilities"
JOB_URI_PREFIX = "job://"
JOB_MIME_TYPE = "application/yaml"
# The JSON-RPC code for a resource that does not exist.
RESOURCE_NOT_FOUND = -32002
DEFAULT_WAIT_TIMES = WaitTimes()
WRITES_OFF = {"code": "writes_off", "message": "Deleting executions through MCP requires dtc serve --allow-write"}


class Connection:
    """One client's connection, from the server's lifespan: the session of a client on a protocol of 2025-11-25 or
    earlier, which a list-changed notification reaches directly. A 2026-07-28 client listens on the bus instead."""

    def __init__(self) -> None:
        self.legacy_session: ServerSession | None = None


class _Server(Server[Connection]):
    """Declares ``tools.listChanged`` to a client of the older handshake, on every transport; a 2026-07-28 client is
    told it because ``subscriptions/listen`` is served."""

    def create_initialization_options(self, notification_options: NotificationOptions | None = None, experimental_capabilities: dict[str, dict[str, Any]] | None = None, extensions: dict[str, dict[str, Any]] | None = None) -> InitializationOptions:
        return super().create_initialization_options(notification_options or NotificationOptions(tools_changed=True), experimental_capabilities, extensions)


class McpApp:
    """The handlers, and what they share: the API client, the last read of whether writes are on, and the open
    connections, for list-changed notifications."""

    def __init__(self, api: ApiClient, *, clock: Callable[[], float], wait_times: WaitTimes) -> None:
        self.api = api
        self._clock = clock
        self._wait_times = wait_times
        self._bus = InMemorySubscriptionBus()
        self._connections: set[Connection] = set()
        # The last read of allow_write (None when it failed), when it was made, and the setting the last tool list
        # given followed (None before the first).
        self._writes: bool | None = None
        self._read_at: float | None = None
        self._listed_writes: bool | None = None

    def build(self) -> Server[Connection]:
        return _Server(
            NAME,
            version=_version(),
            instructions=INSTRUCTIONS,
            cache_hints={"tools/list": LIST_CACHE_HINT, "resources/list": LIST_CACHE_HINT},
            lifespan=self._lifespan,
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
            on_list_resources=self._list_resources,
            on_list_resource_templates=self._list_resource_templates,
            on_read_resource=self._read_resource,
            on_subscriptions_listen=ListenHandler(self._bus),
        )

    @asynccontextmanager
    async def _lifespan(self, _server: Server[Connection]) -> AsyncIterator[Connection]:
        connection = Connection()
        self._connections.add(connection)
        try:
            yield connection
        finally:
            self._connections.discard(connection)
            # The HTTP client's connections close with the last client's; a later client opens them again.
            if not self._connections:
                await self.api.aclose()

    def _remember(self, ctx: ServerRequestContext[Connection]) -> None:
        if ctx.protocol_version not in MODERN_PROTOCOL_VERSIONS:
            ctx.lifespan_context.legacy_session = ctx.session

    async def _read_writes(self) -> bool:
        """``allow_write`` from ``GET /v1/capabilities``, read now; a ``ToolError`` when it cannot be read."""
        self._read_at = self._clock()
        self._writes = None
        return self._note_writes(await self.api.get(CAPABILITIES_PATH, timeout=CAPABILITIES_TIMEOUT_SECONDS))

    def _note_writes(self, capabilities: dict[str, Any]) -> bool:
        self._read_at = self._clock()
        self._writes = capabilities.get("allow_write") is True
        return self._writes

    async def _writes_on(self) -> bool | None:
        """Whether writes are on, read again when the last read is over ``CAPABILITIES_FRESH_SECONDS`` old or failed;
        None when it cannot be read."""
        if self._writes is None or self._read_at is None or self._clock() - self._read_at > CAPABILITIES_FRESH_SECONDS:
            try:
                await self._read_writes()
            except ToolError:
                return None
        return self._writes

    async def _announce(self, writes: bool | None) -> None:
        """Tell every client the tool list changed when ``writes``, read from the API, differs from the list last given:
        on the bus, which reaches 2026-07-28 clients' listen streams, and on each older client's session."""
        if writes is None or self._listed_writes is None or writes == self._listed_writes:
            return
        self._listed_writes = writes
        logger.info("dtc serve's writes are now {}; telling clients the tool list changed", "on" if writes else "off")
        await self._bus.publish(ToolsListChanged())
        for connection in list(self._connections):
            if connection.legacy_session is not None:
                try:
                    await connection.legacy_session.send_tool_list_changed()
                except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError) as error:
                    logger.debug("A client's tool list notification failed: {}", error)

    async def _list_tools(self, ctx: ServerRequestContext[Connection], _params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        self._remember(ctx)
        try:
            writes = await self._read_writes()
        except ToolError:
            writes = False
        else:
            # The other open connections hear of a change this list is the first to see.
            await self._announce(writes)
        self._listed_writes = writes
        return types.ListToolsResult(tools=[_listing(tool) for tool in listed_tools(writes)])

    async def _call_tool(self, ctx: ServerRequestContext[Connection], params: types.CallToolRequestParams) -> types.CallToolResult:
        self._remember(ctx)
        tool = TOOLS.get(params.name)
        if tool is None:
            raise MCPError(types.INVALID_PARAMS, f"Unknown tool: {params.name}")
        try:
            arguments = check_arguments(tool, params.arguments or {})
            request = tool.build(arguments)
            if tool.gated:
                # Read afresh, always: deleting executions is behind --allow-write in MCP alone, though its endpoint
                # is always on, so the MCP server is what refuses it.
                writes = await self._read_writes()
                await self._announce(writes)
                if not writes:
                    raise ToolError(dict(WRITES_OFF))
            elif request.path != CAPABILITIES_PATH:
                await self._announce(await self._writes_on())
            if "wait_seconds" in arguments:
                body = await wait_for_change(self.api, request.path, arguments["wait_seconds"], report=ctx.session.report_progress, has_progress_token=_has_progress_token(ctx), times=self._wait_times)
            else:
                body = await self.api.send(request)
            if request.path == CAPABILITIES_PATH:
                await self._announce(self._note_writes(body))
        except ToolError as error:
            return _result(error.body, is_error=True)
        return _result(body)

    async def _list_resources(self, ctx: ServerRequestContext[Connection], _params: types.PaginatedRequestParams | None) -> types.ListResourcesResult:
        """Every job file, from every page of ``GET /v1/jobs``: they come from the API, never from disk."""
        self._remember(ctx)
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            try:
                page = await self.api.get("/v1/jobs", {"limit": 200, "cursor": cursor})
            except ToolError as error:
                raise _resource_error(error) from error
            rows.extend(page.get("jobs", []))
            cursor = page.get("cursor")
            if cursor is None:
                break
        return types.ListResourcesResult(resources=[types.Resource(name=row["file_name"], uri=job_uri(row["file_name"]), mime_type=JOB_MIME_TYPE, description=f"Job file {row['job_id']}, read-only") for row in rows])

    async def _list_resource_templates(self, ctx: ServerRequestContext[Connection], _params: types.PaginatedRequestParams | None) -> types.ListResourceTemplatesResult:
        self._remember(ctx)
        return types.ListResourceTemplatesResult(resource_templates=[types.ResourceTemplate(name="job", uri_template=f"{JOB_URI_PREFIX}{{job}}", mime_type=JOB_MIME_TYPE, description="A job file's YAML text, read-only; {job} is its ID (J0003) or file name")])

    async def _read_resource(self, ctx: ServerRequestContext[Connection], params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
        self._remember(ctx)
        uri = str(params.uri)
        if not uri.startswith(JOB_URI_PREFIX):
            raise MCPError(types.INVALID_PARAMS, f"Unknown resource {uri}; job files are {JOB_URI_PREFIX}{{job}}")
        try:
            body = await self.api.get(f"/v1/jobs/{path_segment('uri', unquote(uri.removeprefix(JOB_URI_PREFIX)))}")
        except ToolError as error:
            raise _resource_error(error) from error
        return types.ReadResourceResult(contents=[types.TextResourceContents(uri=uri, mime_type=JOB_MIME_TYPE, text=str(body.get("text", "")))])


def _has_progress_token(ctx: ServerRequestContext[Connection]) -> bool:
    """Whether the client asked for progress on this call: only then can progress keep a long wait alive."""
    return ctx.meta is not None and ctx.meta.get("progress_token") is not None


def job_uri(job: str) -> str:
    return f"{JOB_URI_PREFIX}{quote(job, safe='')}"


def _resource_error(error: ToolError) -> MCPError:
    """The API's error as a JSON-RPC error, with its body as the data: a resource has no error result."""
    code = RESOURCE_NOT_FOUND if error.code == "not_found" else types.INVALID_PARAMS if error.code == "invalid_input" else types.INTERNAL_ERROR
    return MCPError(code, str(error), data=error.body)


def _listing(tool: Tool) -> types.Tool:
    return types.Tool(name=tool.name, description=tool.description, input_schema=tool.input_schema, annotations=tool.annotations)


def _result(body: dict[str, Any], *, is_error: bool = False) -> types.CallToolResult:
    """The API's JSON as it is: the structured content, and the same, compact, as the one text block, for a client
    that reads only text."""
    text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], structured_content=body, is_error=is_error)


def _version() -> str:
    try:
        return version("draw-things-control")
    except PackageNotFoundError:
        return "0.0.0"


def build_server(server_url: str, token_path: Path, *, http_transport: httpx.AsyncBaseTransport | None = None, clock: Callable[[], float] = time.monotonic, wait_times: WaitTimes = DEFAULT_WAIT_TIMES) -> Server[Connection]:
    """The MCP server for ``dtc serve`` at ``server_url``, whose token is in ``token_path``. Nothing is read until the
    first request. ``http_transport`` replaces the network, and ``clock`` and ``wait_times`` the times, for tests."""
    return McpApp(ApiClient(server_url, token_path, transport=http_transport), clock=clock, wait_times=wait_times).build()


def run(server_url: str, token_path: Path) -> None:
    """Serve MCP on stdin and stdout until the client closes them."""
    anyio.run(_serve_stdio, server_url, token_path)


async def _serve_stdio(server_url: str, token_path: Path) -> None:
    server = build_server(server_url, token_path)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run_http(server_url: str, token_path: Path, host: str, port: int) -> None:
    """Serve MCP over Streamable HTTP at ``http://host:port/mcp`` until a signal stops it; ``BindError`` when the address cannot be bound."""
    anyio.run(_serve_http, server_url, token_path, host, port)


async def _serve_http(server_url: str, token_path: Path, host: str, port: int) -> None:
    await serve_http(build_server(server_url, token_path), token_path, host, port)
