"""The Execution History widget's rows."""

from __future__ import annotations

from datetime import datetime

from rich.text import Text

from draw_things_control.state.executions import ExecutionRow
from draw_things_control.tui.text.common import STATUS_STYLE

# Before the ID of a row marked for deletion (Milestone 06): a prefix, not a column of its own, which would widen
# the pane past its narrowest (36 columns) with no rows at all.
MARK = "*"


def history_cells(row: ExecutionRow, *, marked: bool = False) -> tuple[Text, ...]:
    """The pane's row: ID (after the mark, when marked), job name, status, start time, and runs succeeded of total."""
    status = row.status
    return (Text.assemble((MARK, "bold yellow"), row.label) if marked else Text(row.label), Text(row.job_name), Text(status, style=STATUS_STYLE.get(status, "")), Text(_started(row, "%m-%d %H:%M")), Text(_runs(row)))


def delete_summary(row: ExecutionRow) -> str:
    """An execution as the delete dialog names it: ``E0012 (walk, succeeded, 7/7 runs, 2026-09-28 14:03)``."""
    return f"{row.label} ({row.job_name}, {row.status}, {_runs(row)} runs, {_started(row, '%Y-%m-%d %H:%M')})"


def _started(row: ExecutionRow, form: str) -> str:
    try:
        return datetime.fromisoformat(row.started_at).astimezone().strftime(form)
    except (TypeError, ValueError):
        return str(row.started_at)


def _runs(row: ExecutionRow) -> str:
    """Runs succeeded of total. A resumed execution's own runs start at first_run, so its own succeeded count
    understates the chain's true progress; the runs before first_run already succeeded, in whichever execution ran them."""
    total = row.total_runs if row.total_runs is not None else "?"
    return f"{row.first_run - 1 + row.succeeded}/{total}"
