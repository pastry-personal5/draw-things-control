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
    # (step, steps) from a "3 / 20" style counter in the line, if any.
    progress: tuple[int, int] | None = None
    # 0-100 from a "45%" style figure in the line, if any; covers stages that have no step counter.
    percent: int | None = None


MessageCallback = Callable[[ProcessMessage], None]
# A "[1/3]" file counter is not a step counter, so a number right after "[" is skipped.
PROGRESS_PATTERN = re.compile(r"(?<![\d\[])(\d+)\s*/\s*(\d+)(?!\d)")
PERCENT_PATTERN = re.compile(r"(?<!\d)(\d{1,3})\s*%")
# Terminal control sequences: CSI (cursor moves, line clears, colors), OSC and APC strings (inline images, titles), and lone two-byte escapes.
ANSI_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[\]P^_].*?(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def strip_terminal_codes(text: str) -> str:
    """Remove escape sequences and stray carriage returns, keeping what a person would read."""
    return ANSI_PATTERN.sub("", text).replace("\r", "")


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
        """Record and log one output line without losing its source stream.

        draw-things-cli redraws its progress bar by printing ``ESC[1A ESC[K`` (cursor up, clear line) before each
        line and a bare newline before the first, so the codes are stripped from what is kept and a line left empty
        by that is dropped. The bar's step counter and percentage are parsed from the cleaned text.
        """
        clean_text = strip_terminal_codes(text.rstrip("\r\n")).rstrip()
        if not clean_text.strip():
            return
        message = ProcessMessage(stream=stream, text=clean_text, elapsed_seconds=elapsed_seconds, progress=self._find_progress(clean_text), percent=self._find_percent(clean_text))
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

    @staticmethod
    def _find_percent(text: str) -> int | None:
        match = PERCENT_PATTERN.search(text)
        if match is None:
            return None
        percent = int(match.group(1))
        return percent if percent <= 100 else None
