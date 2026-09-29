"""How a park reservation and its outcome are worded (Milestone 05), shared by ``dtc queue`` and the TUI, so both word
the same outcome the same way; only the release command a hint names differs. It imports nothing of the worker, so a
front end loads none of it for the words."""

from __future__ import annotations

from typing import Any


def queue_state_text(row: dict[str, Any]) -> str:
    """An entry's state as a front end shows it: ``parking`` for a running entry with a park reservation."""
    return "parking" if row["state"] == "running" and row.get("park_requested") else row["state"]


def park_point(run: int | None, total: int | None) -> str | None:
    """Where a park reservation takes effect, from the run in progress, or the run a cooldown between runs follows (the
    park ends that cooldown at once): ``run 3/7``, or ``its next run`` before any run has started. None when that run
    is the job's last: the job then finishes ``succeeded``, since no run is left to park before, and only the hold
    remains."""
    if run is None:
        return "its next run"
    if run == total:
        return None
    return f"run {run}/{total}" if total is not None else f"run {run}"


def park_outcome_text(entry: dict[str, Any], release_command: str) -> str:
    """What a park did, from the entry the API returned: it may already read ``parked`` when the park ended a cooldown
    between runs, and ``between_runs_after_run`` names the run the entry is between runs after. ``release_command`` is
    the front end's own (``dtc queue release``, ``/queue release``)."""
    queue_id, total = entry["queue_id"], entry.get("total_runs")
    held = f"; the queue is held ('{release_command}' starts it again)" if entry.get("held") else ""
    if entry["state"] == "parked":
        return f"{queue_id} parked after run {entry['succeeded']}/{total}{held}"
    if entry["state"] != "running":
        return f"{queue_id} {entry['state']}"
    point = park_point(entry.get("current_run") or entry.get("between_runs_after_run"), total)
    return f"{queue_id} " + (f"parks after {point}" if point is not None else "is on its last run and will finish") + held


def unpark_outcome_text(entry: dict[str, Any]) -> str:
    """What an unpark did: the job runs on, and the queue either stays held or is not held. It never says a hold was
    released, since an unpark on an entry with no reservation, or whose hold another made, releases none."""
    return f"{entry['queue_id']} runs on; the queue " + ("stays held" if entry.get("held") else "is not held")
