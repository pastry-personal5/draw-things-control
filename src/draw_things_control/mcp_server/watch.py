"""``get_queue_entry``'s wait (Milestone 10): it reads the entry, and, unless it is finished, follows the API's SSE
watch of it (``GET /v1/queue/{id}/watch``) until the next change an agent acts on, usually a run's end, or the time
is up, then reads the entry again. Each call is a turn that reads the agent's whole conversation again, so a wait that
ends at the next change takes about one call a run. ``mcp_server/`` imports nothing from the package, so the finished
states are a copy of ``state/queue.py``'s (a test checks the two agree)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anyio

from draw_things_control.mcp_server.api import ApiClient, ToolError

FINISHED_STATES = frozenset({"succeeded", "failed", "cancelled", "interrupted", "parked"})
# The snapshot fields an agent acts on: a change in one ends a wait. current_run is apart: only its end counts.
ACTED_ON_FIELDS = ("state", "execution_id", "error", "park_requested", "queue_held", "cooldown_until")
SNAPSHOT_EVENT = "snapshot"

# Reports progress on the call: (progress, total, message). The session's own, which does nothing without a token.
ReportProgress = Callable[[float, float | None, str | None], Awaitable[None]]


@dataclass(frozen=True)
class WaitTimes:
    """How often a wait reports progress; the longest it waits for a call that came without a progress token, which
    nothing then keeps alive past Claude Code's 30-minute idle limit for a stdio server; and how long the watch may
    stay silent, keep-alives included, before it counts as dropped. Tests shorten them."""

    progress_every: float = 15.0
    without_progress_token: float = 1500.0
    silence: float = 60.0


def acted_on(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """Whether ``current``, the snapshot after ``previous``, changes what an agent acts on: a field it reads, or the
    end of a run, ``current_run`` leaving a run's number (for null between runs, or straight for the next one when no
    cooldown parts them and both land between two polls). A run's start from null does not count, since the worker
    clears ``current_run`` between runs and sets it again after the cooldown; nor does the step, which changes all
    through a run."""
    if any(previous.get(name) != current.get(name) for name in ACTED_ON_FIELDS):
        return True
    return previous.get("current_run") is not None and current.get("current_run") != previous.get("current_run")


def as_snapshot(entry: dict[str, Any]) -> dict[str, Any]:
    """The fields of ``GET /v1/queue/{id}``'s entry that a snapshot carries, under a snapshot's names (the hold is
    ``held`` there), so the read before the watch is the first thing its baseline is compared with."""
    return {**{name: entry.get(name) for name in ACTED_ON_FIELDS if name != "queue_held"}, "queue_held": entry.get("held"), "current_run": entry.get("current_run")}


async def wait_for_change(api: ApiClient, entry_path: str, wait_seconds: float, *, report: ReportProgress, has_progress_token: bool, times: WaitTimes) -> dict[str, Any]:
    """``GET entry_path`` once the entry changes, or ``wait_seconds`` pass, with ``changed`` added. A finished entry is
    answered at once with ``changed: false``; nothing more will change."""
    entry = await api.get(entry_path)
    if entry.get("state") in FINISHED_STATES:
        return {**entry, "changed": False}
    read = as_snapshot(entry)
    limit = wait_seconds if has_progress_token else min(wait_seconds, times.without_progress_token)
    changed = False
    failure: ToolError | None = None
    with anyio.move_on_after(limit):
        async with anyio.create_task_group() as group:
            group.start_soon(_keep_alive, report, limit, times.progress_every)
            # Caught here and raised once the group has ended, so it is not wrapped in an exception group.
            try:
                changed = await _next_change(api, entry_path, read, times.silence)
            except ToolError as error:
                failure = error
            group.cancel_scope.cancel()
    if failure is not None:
        raise failure
    return {**await api.get(entry_path), "changed": changed}


async def _keep_alive(report: ReportProgress, limit: float, every: float) -> None:
    """A progress notification every ``every`` seconds, so a client that restarts its timer on progress does not end
    the call. A client gone stops it; the call's own cancellation follows."""
    started = anyio.current_time()
    while True:
        await anyio.sleep(every)
        try:
            await report(min(anyio.current_time() - started, limit), limit, "Waiting for the entry to change")
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            return


async def _next_change(api: ApiClient, entry_path: str, read: dict[str, Any], silence: float) -> bool:
    """True at the first snapshot that changes what an agent acts on, compared with the one before it, and the first
    (the watch's current snapshot) with ``read``, the entry as read before the watch opened, so a change between the
    two, the entry finishing included, is not missed. A watch that ends first is the API's answer to a read of the
    entry, ``not_found`` once it is gone, or ``server_unreachable`` when the server stops."""
    async with api.stream(f"{entry_path}/watch", silence=silence) as lines:
        previous = read
        async for event, data in sse_messages(lines):
            if event != SNAPSHOT_EVENT:
                continue
            try:
                snapshot = json.loads(data)
            except json.JSONDecodeError as error:
                raise ToolError({"code": "error", "message": f"dtc serve at {api.server_url} sent a watch snapshot that is not JSON"}) from error
            if acted_on(previous, snapshot):
                return True
            previous = snapshot
    await api.get(entry_path)
    raise api.unreachable("it ended the watch")


async def sse_messages(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, str]]:
    """Each message of an SSE stream as (event, data): ``event:`` and ``data:`` lines, a blank line ending each, a
    comment line (``: keep-alive``) ignored."""
    event, data = "message", []
    async for line in lines:
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
        elif not line.startswith(":"):
            name, _colon, value = line.partition(":")
            value = value.removeprefix(" ")
            if name == "event":
                event = value
            elif name == "data":
                data.append(value)
