"""Classify, retain, and log output from a child process."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from loguru import logger


class OutputStream(str, Enum):
    """The source stream for a process message."""

    STDOUT = "stdout"
    STDERR = "stderr"


@dataclass(frozen=True)
class ProcessMessage:
    """One timestamped line emitted by the supervised command."""

    stream: OutputStream
    text: str
    elapsed_seconds: float
    progress: tuple[int, int] | None = None


MessageCallback = Callable[[ProcessMessage], None]
PROGRESS_PATTERN = re.compile(r"(?<!\d)(\d+)\s*/\s*(\d+)(?!\d)")


class OutputProcessor:
    """Formats, stores, and publishes interleaved process output."""

    def __init__(self, *, callback: MessageCallback | None = None, max_messages: int = 5_000) -> None:
        if max_messages < 1:
            raise ValueError("max_messages must be at least 1")
        self._callback = callback
        self._messages: deque[ProcessMessage] = deque(maxlen=max_messages)

    @property
    def messages(self) -> tuple[ProcessMessage, ...]:
        """Return the retained output, oldest message first."""
        return tuple(self._messages)

    def process(self, stream: OutputStream, text: str, elapsed_seconds: float) -> None:
        """Record and log one output line without losing its source stream."""
        clean_text = text.rstrip("\r\n")
        message = ProcessMessage(stream=stream, text=clean_text, elapsed_seconds=elapsed_seconds, progress=self._find_progress(clean_text))
        self._messages.append(message)
        level = "WARNING" if stream is OutputStream.STDERR else "INFO"
        logger.bind(child_stream=stream.value).log(level, "[{} +{:.1f}s] {}", stream.value, elapsed_seconds, clean_text)
        if self._callback is not None:
            self._callback(message)

    @staticmethod
    def _find_progress(text: str) -> tuple[int, int] | None:
        match = PROGRESS_PATTERN.search(text)
        if match is None:
            return None
        current, total = (int(value) for value in match.groups())
        return (current, total) if total > 0 and current <= total else None
