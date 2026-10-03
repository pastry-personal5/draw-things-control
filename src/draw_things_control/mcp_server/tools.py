"""The MCP server's tools (Milestone 10): each one's name, arguments and input schema (with the API's names and bounds),
description, and annotations, the endpoint it calls, and the check of a call's arguments against its schema. A tool
returns the API's JSON as it is; the one exception to calling an endpoint as given is ``delete_executions``, which the
server refuses itself while writes are off (``app.py``)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mcp_types import ToolAnnotations

from draw_things_control.mcp_server.api import ApiRequest, invalid_input, path_segment

# What a list tool asks for when the agent gives no limit: a page of 200 executions is about 70 KB, near the size past
# which Claude Code saves a result to a file for the agent to read in parts. The API's own bounds stand.
DEFAULT_LIMIT = 50
MAX_IDS = 200
WAIT_SECONDS_MAX = 7200

Arguments = dict[str, Any]
# Builds a tool's request from its checked arguments, refusing a path argument before anything is sent.
Build = Callable[[Arguments], ApiRequest]

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
RUN = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
IDEMPOTENT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


@dataclass(frozen=True)
class Tool:
    """One tool. ``write`` tools are listed only while the API server has writes on; a ``gated`` one is also refused
    by the MCP server itself unless a fresh read says writes are on, since its endpoint is always on."""

    name: str
    description: str
    build: Build
    annotations: ToolAnnotations
    properties: dict[str, dict[str, Any]] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    write: bool = False
    gated: bool = False

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": self.properties, "required": list(self.required), "additionalProperties": False}


def check_arguments(tool: Tool, arguments: Arguments) -> Arguments:
    """``arguments`` checked against ``tool``'s schema: no unknown argument, every required one, each of its type, and
    the bounds the API refuses rather than clamps. A refusal is ``invalid_input`` naming the argument. An optional
    argument given as null is left out, and an integer may come as a whole number with a decimal point (``50.0``), as
    JSON Schema allows."""
    for name in arguments:
        if name not in tool.properties:
            raise invalid_input(name, f"Unknown argument '{name}'; {tool.name} takes {', '.join(sorted(tool.properties)) or 'no arguments'}")
    checked = {name: _check_value(name, value, tool.properties[name]) for name, value in arguments.items() if value is not None or name in tool.required}
    for name in tool.required:
        if name not in checked:
            raise invalid_input(name, f"'{name}' is required")
    return checked


def _check_value(name: str, value: Any, schema: dict[str, Any]) -> Any:
    kind = schema["type"]
    if kind == "string" and not isinstance(value, str):
        raise invalid_input(name, f"'{name}' must be a string")
    if kind == "boolean" and not isinstance(value, bool):
        raise invalid_input(name, f"'{name}' must be true or false")
    if kind == "integer":
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise invalid_input(name, f"'{name}' must be an integer")
        if "minimum" in schema and value < schema["minimum"]:
            raise invalid_input(name, f"'{name}' must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise invalid_input(name, f"'{name}' must be at most {schema['maximum']}")
    if kind == "array":
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise invalid_input(name, f"'{name}' must be a list of strings")
        if not schema["minItems"] <= len(value) <= schema["maxItems"]:
            raise invalid_input(name, f"'{name}' must name {schema['minItems']} to {schema['maxItems']} items")
    return value


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


LIMIT = {"type": "integer", "minimum": 1, "description": f"Page size; {DEFAULT_LIMIT} when not given, at most 200 returned"}
CURSOR = _string("The cursor the previous page returned")
JOB = _string("A job's ID (J0003) or file name in data/jobs/ (walk.yaml)")
QUEUE_ID = _string("A queue entry's ID (Q0007)")
EXECUTION_ID = _string("An execution's ID (E0012)")
NAME = _string("The job file's name in data/jobs/ without .yaml: 1 to 64 lowercase letters, digits, or hyphens")
YAML = _string("The job file's whole YAML text")
SHA256 = _string("The sha256 get_job (brief: false) last returned for the file")


def _page(arguments: Arguments) -> dict[str, Any]:
    return {"limit": arguments.get("limit", DEFAULT_LIMIT), "cursor": arguments.get("cursor")}


def _get(path: Callable[[Arguments], str], params: Callable[[Arguments], dict[str, Any]] | None = None) -> Build:
    return lambda arguments: ApiRequest("GET", path(arguments), params(arguments) if params is not None else None)


def _post(path: Callable[[Arguments], str], body: Callable[[Arguments], Any] | None = None) -> Build:
    return lambda arguments: ApiRequest("POST", path(arguments), body=body(arguments) if body is not None else None)


def _queue(suffix: str = "") -> Callable[[Arguments], str]:
    return lambda arguments: f"/v1/queue/{path_segment('queue_id', arguments['queue_id'])}{suffix}"


def _execution(suffix: str = "") -> Callable[[Arguments], str]:
    return lambda arguments: f"/v1/executions/{path_segment('execution_id', arguments['execution_id'])}{suffix}"


def _job(suffix: str = "", argument: str = "job") -> Callable[[Arguments], str]:
    return lambda arguments: f"/v1/jobs/{path_segment(argument, arguments[argument])}{suffix}"


def _validate(arguments: Arguments) -> ApiRequest:
    return ApiRequest("POST", "/v1/validate", {"name": arguments.get("name")}, {"yaml": arguments["yaml"]})


def _create(arguments: Arguments) -> ApiRequest:
    return ApiRequest("PUT", _job(argument="name")(arguments), body={"yaml": arguments["yaml"]})


def _replace(arguments: Arguments) -> ApiRequest:
    return ApiRequest("PUT", _job(argument="name")(arguments), {"overwrite": "1"}, {"yaml": arguments["yaml"], "expected_sha256": arguments["expected_sha256"]})


def _delete_job(arguments: Arguments) -> ApiRequest:
    return ApiRequest("DELETE", _job(argument="name")(arguments), {"expected_sha256": arguments["expected_sha256"]})


def _delete_executions(arguments: Arguments) -> ApiRequest:
    return ApiRequest("POST", "/v1/executions/delete", body={"executions": arguments["executions"], "dry_run": arguments["dry_run"]})


READ_AND_RUN = (
    Tool("get_capabilities", "Whether writes are on, and the limits every job must meet (max_job_runs, max_job_seconds, max_queued_jobs, max_job_file_bytes). The write tools (create_job, replace_job, delete_job, delete_executions) are listed only while dtc serve runs with --allow-write.", _get(lambda _a: "/v1/capabilities"), READ),
    Tool("list_jobs", "The job files in data/jobs/: ID, file name, name, mode, runs, and validity.", _get(lambda _a: "/v1/jobs", _page), READ, {"limit": LIMIT, "cursor": CURSOR}),
    Tool(
        "get_job",
        "A job resolved: prompt pairs, size, seed, cooldown, and sha256. Brief unless brief is false, which adds the YAML text an edit needs; an invalid job always has its text and error.",
        _get(_job(), lambda arguments: {"brief": "1" if arguments.get("brief", True) else None}),
        READ,
        {"job": JOB, "brief": {"type": "boolean", "description": "Leave out a valid job's YAML text (default true)"}},
        ("job",),
    ),
    Tool("preview_job", "A job's dry-run plan: each run's input, output, and command, without running anything.", _get(_job("/preview")), READ, {"job": JOB}, ("job",)),
    Tool(
        "validate_job_text",
        "Check a draft job's YAML without writing it; given name, check it as a write to that name would. Inputs must already be in the input directory and match the job's size (list_inputs), run_timeout_seconds is required, and the limits apply (get_capabilities).",
        _validate,
        READ,
        {"yaml": YAML, "name": NAME},
        ("yaml",),
    ),
    Tool("list_inputs", "The images in the input directory a job can use: path, size in bytes, width, and height.", _get(lambda _a: "/v1/inputs", _page), READ, {"limit": LIMIT, "cursor": CURSOR}),
    Tool("submit_job", "Queue a job file; what runs is a snapshot of it as it is now. People share the queue.", _post(lambda _a: "/v1/queue", lambda arguments: {"job": arguments["job"]}), RUN, {"job": JOB}, ("job",)),
    Tool("get_queue", "The queue: queued and running entries in full, then finished ones newest first, paged, with the worker's state and the hold.", _get(lambda _a: "/v1/queue", lambda arguments: {**_page(arguments), "state": arguments.get("state")}), READ, {"state": _string("Only entries in this state: queued, running, succeeded, failed, cancelled, interrupted, or parked"), "limit": LIMIT, "cursor": CURSOR}),
    Tool(
        "get_queue_entry",
        "One queue entry: its state, run, step, cooldown, the hold, and whether and from which run it can be resumed. With wait_seconds, it answers at the next change you act on, usually a run's end, so one call with a long wait follows a run, and adds changed (false when the time was up, or the entry had finished). A client that ends a tool call after 60 seconds, as many do, needs 50 or less.",
        _get(_queue()),
        READ,
        {"queue_id": QUEUE_ID, "wait_seconds": {"type": "integer", "minimum": 1, "maximum": WAIT_SECONDS_MAX, "description": f"Wait up to this long, 1 to {WAIT_SECONDS_MAX} seconds, for the entry to change"}},
        ("queue_id",),
    ),
    Tool("cancel_queue_entry", "Cancel an entry you submitted. A running one stops at once and loses the run in progress; park_queue_entry keeps it.", _post(_queue("/cancel")), DESTRUCTIVE, {"queue_id": QUEUE_ID}, ("queue_id",)),
    Tool("resume_queue_entry", "Continue a stopped chain you submitted (interrupted, failed, cancelled, or parked) as a new entry, rerunning the run that was cut short; get_queue_entry says from which run.", _post(_queue("/resume")), RUN, {"queue_id": QUEUE_ID}, ("queue_id",)),
    Tool("list_executions", "Executions in the history, newest first, paged.", _get(lambda _a: "/v1/executions", lambda arguments: {**_page(arguments), "status": arguments.get("status"), "job": arguments.get("job"), "name": arguments.get("name")}), READ, {"status": _string("Only executions with this status"), "job": JOB, "name": _string("Only executions whose job or file name contains this text"), "limit": LIMIT, "cursor": CURSOR}),
    Tool("get_execution", "An execution in brief: its runs, and each check's stage and verdict. Each run's command and each check's details come from get_execution_run.", _get(_execution(), lambda _a: {"brief": "1"}), READ, {"execution_id": EXECUTION_ID}, ("execution_id",)),
    Tool("get_execution_run", "One run of an execution in full: its command and its checks with their facts. run is its number in the chain.", _get(lambda arguments: f"{_execution('/runs/')(arguments)}{arguments['run']}"), READ, {"execution_id": EXECUTION_ID, "run": {"type": "integer", "minimum": 1, "description": "The run's number in the chain"}}, ("execution_id", "run")),
    Tool("list_outputs", "Each run's output and last-frame paths, whether they exist, and their size. An output marked incomplete is a leftover, not a result.", _get(_execution("/outputs")), READ, {"execution_id": EXECUTION_ID}, ("execution_id",)),
)

QUEUE_CONTROL = (
    Tool("park_queue_entry", "Park a running entry you submitted: it ends after its current run with nothing lost, and the queue is held, so nothing else starts, its own resume included, until release_queue.", _post(_queue("/park")), RUN, {"queue_id": QUEUE_ID}, ("queue_id",)),
    Tool("unpark_queue_entry", "Withdraw a park before its run ends; it ends the hold if the park made it.", _post(_queue("/unpark")), RUN, {"queue_id": QUEUE_ID}, ("queue_id",)),
    Tool("hold_queue", "Hold the queue: the running job finishes, and nothing starts after it until release_queue. A second call answers changed: false.", _post(lambda _a: "/v1/queue/hold"), IDEMPOTENT),
    Tool("release_queue", "End a hold an agent made: the oldest queued entry, perhaps a person's, starts at once. A second call answers changed: false.", _post(lambda _a: "/v1/queue/release"), IDEMPOTENT),
)

WRITE = (
    Tool("create_job", "Write a new job file to data/jobs/; refused when the name is taken or the job is invalid (validate_job_text first).", _create, RUN, {"name": NAME, "yaml": YAML}, ("name", "yaml"), write=True),
    Tool(
        "replace_job",
        "Replace a job file, keeping a backup. expected_sha256 is the sha256 get_job (brief: false) last returned; a conflict with current_sha256 means a person changed the file since: read it again and redo the edit. A queued or running job's file cannot be changed.",
        _replace,
        DESTRUCTIVE,
        {"name": NAME, "yaml": YAML, "expected_sha256": SHA256},
        ("name", "yaml", "expected_sha256"),
        write=True,
    ),
    Tool("delete_job", "Move a job file to the trash. expected_sha256 is the sha256 get_job last returned; a queued or running job's file cannot be deleted.", _delete_job, DESTRUCTIVE, {"name": NAME, "expected_sha256": SHA256}, ("name", "expected_sha256"), write=True),
    Tool(
        "delete_executions",
        "Delete executions for good: each one's row, runs, log, and manifest; outputs stay. Call it with dry_run true first: the answer says what would be deleted, what is refused (a running one, or one a queued or running entry uses), and which parked or failed entries could no longer be resumed.",
        _delete_executions,
        DESTRUCTIVE,
        {"executions": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": MAX_IDS, "description": "1 to 200 execution IDs (E0012)"}, "dry_run": {"type": "boolean", "description": "true: only say what a deletion would do"}},
        ("executions", "dry_run"),
        write=True,
        gated=True,
    ),
)

TOOLS = {tool.name: tool for tool in (*READ_AND_RUN, *QUEUE_CONTROL, *WRITE)}


def listed_tools(writes: bool) -> list[Tool]:
    """The tools listed: the read, run, and queue control ones always, and the write ones while writes are on."""
    return [tool for tool in TOOLS.values() if writes or not tool.write]
