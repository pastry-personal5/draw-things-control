"""Fake runs for the TUI: runners that print lines, report a PID, and optionally block until stopped; never draw-things-cli."""

from __future__ import annotations

import signal
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from draw_things_control.core.draw_things_arguments import DrawThingsGenerateArguments
from draw_things_control.core.process_output import OutputProcessor, OutputStream
from draw_things_control.jobs.job_service import JobService

FAKE_PID = 4242
# Plain lines, a line with brackets, and the progress bar's lines, which the output pane must not hold.
LINES = (
    (OutputStream.STDOUT, "Loading model base.ckpt"),
    (OutputStream.STDERR, "warning: [cache] missing [/bold]"),
    (OutputStream.STDOUT, "Sampling  37%|###      | 3/8 [00:03<00:05]"),
    (OutputStream.STDOUT, "Sampling  50%|####     | 4/8 [00:04<00:04]"),
    (OutputStream.STDOUT, "Saved output"),
)


@dataclass(frozen=True)
class FakeResult:
    return_code: int = 0
    timed_out: bool = False
    termination_signal: signal.Signals | None = None


class ScriptedRunner:
    """Prints ``lines`` through the output processor the real runner uses, then writes its output.

    With ``block``, it runs until asked to stop, as a long generation does; with ``gate``, it waits for the gate after printing.
    """

    def __init__(self, arguments: DrawThingsGenerateArguments, grace: float, on_message: object, on_start: object, *, lines: tuple[tuple[OutputStream, str], ...], block: bool, gate: threading.Event | None, started: threading.Event | None) -> None:
        self.arguments = arguments
        self.grace = grace
        self.processor = OutputProcessor(callback=on_message)  # type: ignore[arg-type]
        self.on_start = on_start
        self.lines = lines
        self.block = block
        self.gate = gate
        self.started = started
        self.shutdown_signal: signal.Signals | None = None
        self.stopped = threading.Event()

    def run(self) -> FakeResult:
        if self.on_start is not None:
            self.on_start(FAKE_PID, "draw-things-cli")  # type: ignore[operator]
        for index, (stream, text) in enumerate(self.lines):
            self.processor.process(stream, text, index * 0.5)
        if self.started is not None:
            self.started.set()
        if self.gate is not None:
            assert self.gate.wait(10), "the test never opened the gate"
        if self.block:
            assert self.stopped.wait(10), "the job never asked the runner to stop"
            return FakeResult(return_code=-self.shutdown_signal.value, termination_signal=self.shutdown_signal)  # type: ignore[union-attr]
        assert self.arguments.output is not None
        self.arguments.output.write_bytes(b"video")
        return FakeResult()

    def request_shutdown(self, received_signal: signal.Signals = signal.SIGTERM) -> None:
        self.shutdown_signal = received_signal
        self.stopped.set()


class FakeRuns:
    """A JobService whose runners are ScriptedRunners; every runner it made is kept in ``runners``."""

    def __init__(self, *, lines: tuple[tuple[OutputStream, str], ...] = LINES, block: bool = False, gate: threading.Event | None = None, missing: frozenset[str] = frozenset()) -> None:
        self.lines = lines
        self.block = block
        self.gate = gate
        self.started = threading.Event()
        self.runners: list[ScriptedRunner] = []
        self.service = JobService(
            runner_factory=self.create_runner,
            find_executable=lambda executable: None if executable in missing else executable,
            frame_extractor=self.extract,
            require_ffmpeg=lambda: "ffmpeg",
            clock=datetime.now,
            random_seed=lambda: 777,
            handle_signals=False,
        )

    def create_runner(self, arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float, on_message: object = None, on_start: object = None) -> ScriptedRunner:
        runner = ScriptedRunner(arguments, grace, on_message, on_start, lines=self.lines, block=self.block, gate=self.gate, started=self.started)
        self.runners.append(runner)
        return runner

    @staticmethod
    def extract(video: Path, png: Path) -> None:
        png.write_bytes(b"png")
