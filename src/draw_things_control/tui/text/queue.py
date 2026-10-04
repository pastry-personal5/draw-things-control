"""The Queue widget's rows."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.text import Text

from draw_things_control.services.queue_hold import HoldState, hold_text
from draw_things_control.services.queue_park_text import queue_state_text
from draw_things_control.state.queue import QueueRow
from draw_things_control.tui.text.common import STATUS_STYLE


def queue_row_to_dict(row: QueueRow) -> dict[str, Any]:
    """A stored ``QueueRow`` (the store fallback) as the same shape the API's ``queue_entry()`` gives, so
    ``queue_cells`` renders a row from either source identically."""
    # A park reservation is kept in the server's memory only, so the store never shows one.
    generation = None
    if row.kind == "generate":
        from draw_things_control.services.generation_submit import GenerationSnapshot

        snapshot = GenerationSnapshot.from_json(row.snapshot)
        generation = {"model": snapshot.model, "output": snapshot.output}
    return {"queue_id": row.label, "kind": row.kind, "job_path": row.job_path if row.kind == "job" else None, "generation": generation, "state": row.state, "total_runs": row.total_runs, "succeeded": row.succeeded, "park_requested": False}


def _name(row: dict[str, Any]) -> str:
    generation = row.get("generation")
    return f"generate: {generation['output']}" if isinstance(generation, dict) else Path(str(row["job_path"])).stem


def queue_cells(row: dict[str, Any]) -> tuple[Text, ...]:
    """One row: ID, the job file's name without its extension, state, and runs -- succeeded of total once the entry
    is over, or the run now in progress (``succeeded`` runs already done means run ``succeeded + 1`` is the one
    running: a chain stops the moment a run fails, so a running entry's own succeeded count never lags behind its
    current run by more than the one now in flight)."""
    total = row["total_runs"] if row["total_runs"] is not None else "?"
    succeeded = row["succeeded"]
    runs = f"run {succeeded + 1}/{total}" if row["state"] == "running" else f"{succeeded}/{total}"
    state = queue_state_text(row)
    return (Text(row["queue_id"]), Text(_name(row)), Text(state, style=STATUS_STYLE.get(state, "")), Text(runs))


def queue_listing_text(entries: list[dict[str, Any]], hold: HoldState | None = None) -> Text:
    """/get queue: every entry, in the order given (the running one, then the queued ones, then the finished ones),
    under the hold, when the queue is held."""
    held = hold_text(hold) if hold is not None else ""
    if not entries:
        return Text("\n".join(line for line in (held, "No queue entries.") if line), style="dim")
    text = Text("Queue\n", style="bold")
    if held:
        text.append(f"  {held}\n", style="yellow")
    id_width = max(len(entry["queue_id"]) for entry in entries)
    for entry in entries:
        queue_id, job, state, runs = queue_cells(entry)
        text.append(f"  {str(queue_id).ljust(id_width)}  ")
        text.append_text(job)
        text.append("  ")
        text.append_text(state)
        text.append(f"  {runs}\n")
    text.rstrip()
    return text


def queue_entry_detail_text(entry: dict[str, Any]) -> Text:
    """/describe Q0007 (and Enter on its row): its state, job, times, execution, current run, and whether it can be
    resumed, from which run, or why not."""
    text = Text(f"Queue entry {entry['queue_id']}: ", style="bold")
    state = queue_state_text(entry)
    text.append(state, style=STATUS_STYLE.get(state, ""))
    text.append("\n")
    total = entry.get("total_runs")
    runs = f"{entry['succeeded']}/{total}" if total is not None else None
    if entry["state"] == "running" and entry.get("current_run") is not None:
        runs = f"run {entry['current_run']}/{total}" if total is not None else f"run {entry['current_run']}"
    for label, value in (
        ("Job", _name(entry)),
        ("Submitted", entry.get("submitted_at")),
        ("Started", entry.get("started_at")),
        ("Finished", entry.get("finished_at")),
        ("Execution", entry.get("execution_id")),
        ("Runs", runs),
        ("Resumes", entry.get("resumes")),
        ("Park reservation", "yes" if entry.get("park_requested") else None),
        ("Error", entry.get("error")),
    ):
        if value:
            text.append(f"  {label}: ", style="bold")
            text.append(f"{value}\n")
    if "resumable" in entry:
        text.append("  Resumable: ", style="bold")
        text.append(f"yes, from run {entry['resume_from_run']}\n" if entry["resumable"] else f"no ({entry.get('resume_refused_reason')})\n")
    if entry.get("held"):
        text.append("  Queue: ", style="bold")
        text.append(f"{hold_text(HoldState.from_body(entry)).removeprefix('Queue ')}\n")
    text.rstrip()
    return text
