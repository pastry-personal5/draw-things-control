"""Desktop actions for the history: reveal a run's output in Finder, and copy text to the clipboard."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from draw_things_control.state.executions import ExecutionRow


def reveal_target(execution: ExecutionRow, run_number: int | None) -> Path | str:
    """The output file to reveal for the run (default: the last run with an output), or why there is none."""
    if run_number is None:
        with_output = [run for run in execution.runs if run.output]
        if not with_output:
            return f"Execution {execution.label} has no output"
        run = with_output[-1]
    else:
        found = execution.run(run_number)
        if found is None:
            return f"Execution {execution.label} has no run {run_number}"
        run = found
    path = execution.run_file(run.output)
    if path is None:
        return f"Run {run.number} of execution {execution.label} has no output" if not run.output else f"The output directory of execution {execution.label} was not recorded"
    if not path.exists():
        return f"{path} is missing"
    return path


def reveal_in_finder(path: Path) -> str | None:
    """Select the file in a Finder window; None when it did, else why not. The path is an argument, never shell text."""
    try:
        result = subprocess.run(["open", "-R", str(path)], check=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        return f"Cannot reveal {path}: {error}"
    if result.returncode != 0:
        return f"Cannot reveal {path}: open exited with {result.returncode}{': ' + result.stderr.strip() if result.stderr.strip() else ''}"
    return None


def copy_to_pasteboard(text: str) -> str | None:
    """Put ``text`` on the macOS clipboard with pbcopy; None when it did, else why not (also when there is no pbcopy)."""
    pbcopy = shutil.which("pbcopy")
    if pbcopy is None:
        return "pbcopy was not found"
    # pbcopy reads its input in the locale's encoding; UTF-8 keeps any prompt's characters.
    environment = {**os.environ, "LC_CTYPE": "UTF-8"}
    try:
        result = subprocess.run([pbcopy], input=text.encode("utf-8"), check=False, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10, env=environment)
    except (OSError, subprocess.SubprocessError) as error:
        return f"pbcopy failed: {error}"
    if result.returncode != 0:
        return f"pbcopy exited with {result.returncode}"
    return None


def copy_text(text: str, what: str, *, ask_terminal: Callable[[str], None]) -> tuple[str, str]:
    """Copy ``text`` to the clipboard; the message to say, and its style. Without pbcopy, ``ask_terminal`` asks the terminal
    to copy (OSC 52); not every terminal does, so that cannot be confirmed."""
    error = copy_to_pasteboard(text)
    if error is None:
        return f"Copied the {what} to the clipboard", "dim"
    ask_terminal(text)
    return f"Asked the terminal to copy the {what} ({error})", "dim"


def reveal_run(execution: ExecutionRow | str, run_number: int | None) -> tuple[str, str]:
    """Reveal the run's output in Finder (/reveal, a click, or Enter): the message to say, and its style. ``execution`` is
    as the reader returns it, a string when it could not be read; runs off the UI thread."""
    target = execution if isinstance(execution, str) else reveal_target(execution, run_number)
    if isinstance(target, str):
        return target, "red"
    error = reveal_in_finder(target)
    return (error, "red") if error else (f"Revealed {target}", "")
