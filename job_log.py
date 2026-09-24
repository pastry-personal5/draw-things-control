"""Save a job's full log to a file beside its manifest."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger


def _format(record: dict[str, Any]) -> str:
    # Child lines already start with "[stdout +1.2s]" or "[stderr +1.2s]".
    stream = "" if record["extra"].get("child_stream") else "[app] "
    return "{time:YYYY-MM-DD HH:mm:ss.SSS} " + stream + "{message}\n{exception}"


def add_job_log(path: Path) -> int:
    """Start copying every log message to ``path``; returns the sink id."""
    return logger.add(path, format=_format, level="INFO", colorize=False, buffering=1, encoding="utf-8")


def remove_job_log(sink_id: int) -> None:
    """Stop writing to the job log and close it."""
    logger.remove(sink_id)
