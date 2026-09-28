"""The Execution History widget's rows."""

from __future__ import annotations

from datetime import datetime

from rich.text import Text

from draw_things_control.state.executions import ExecutionRow
from draw_things_control.tui.text.common import STATUS_STYLE


def history_cells(row: ExecutionRow) -> tuple[Text, ...]:
    """The pane's row: ID, job name, status, start time, and runs succeeded of total."""
    try:
        started = datetime.fromisoformat(row.started_at).astimezone().strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        started = str(row.started_at)
    total = row.total_runs if row.total_runs is not None else "?"
    status = row.status
    # A resumed execution's own runs start at first_run, so its own succeeded count understates the chain's true
    # progress; the runs before first_run already succeeded, in whichever execution ran them.
    succeeded = row.first_run - 1 + row.succeeded
    return (Text(row.label), Text(row.job_name), Text(status, style=STATUS_STYLE.get(status, "")), Text(started), Text(f"{succeeded}/{total}"))
