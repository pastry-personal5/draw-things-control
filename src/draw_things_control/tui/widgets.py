"""The TUI's small widgets: the command line, the message log, and the text the one-second tick sets."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self

from rich.text import Text
from textual.binding import Binding
from textual.visual import VisualType
from textual.widgets import Input, RichLog, Static

MAX_MESSAGE_LINES = 5000


class SteadyText(Static):
    """Text of a fixed size (``styles.tcss`` sets its height) that the one-second tick sets again whether or not it
    changed: the Status widget, the run line, and the status line. An unchanged text is not drawn again, and a changed
    one is drawn without a layout pass, which would also redraw the scrollbars below it. While a job runs, draw-things-cli
    keeps the GPU busy and the terminal's own drawing waits for it, so every frame the TUI sends costs the person typing."""

    def __init__(self, *args: Any, **options: Any) -> None:
        super().__init__(*args, **options)
        self._shown: tuple[Any, ...] | None = None

    def show_text(self, text: Text) -> None:
        # Text's own == leaves out its base style (``Text("x", style="dim")``), which may be all that changed. Textual
        # draws only these three: a Rich Text's no_wrap, overflow, and justify never reach the screen.
        shown = (text.plain, text.style, tuple(text.spans))
        if shown == self._shown:
            return
        self.update(text, layout=False)
        self._shown = shown

    def update(self, content: VisualType = "", *, layout: bool = True) -> None:
        # Content set any other way is not what show_text last showed, so its next text is drawn whatever it is.
        self._shown = None
        super().update(content, layout=layout)


class CommandInput(Input):
    """The command line: a steady cursor, Tab completion, Escape to clear, and Up/Down to recall this session's commands."""

    BINDINGS = [
        Binding("tab", "complete", "Complete", show=False),
        Binding("up", "recall(-1)", "Earlier command", show=False),
        Binding("down", "recall(1)", "Later command", show=False),
        Binding("escape", "clear_line", "Clear", show=False),
    ]

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.cursor_blink = False
        # Commands entered in this session, oldest first; kept in memory only.
        self.recalled: list[str] = []
        # The position in ``recalled`` shown now; its length means the line being typed.
        self.recall_index = 0

    def remember(self, line: str) -> None:
        """Keep an entered line for recall, once when it repeats the one before it."""
        if not self.recalled or self.recalled[-1] != line:
            self.recalled.append(line)
        self.recall_index = len(self.recalled)

    def action_recall(self, step: int) -> None:
        index = min(max(self.recall_index + step, 0), len(self.recalled))
        if index == self.recall_index:
            return
        self.recall_index = index
        self.value = self.recalled[index] if index < len(self.recalled) else ""
        self.cursor_position = len(self.value)

    def action_clear_line(self) -> None:
        self.value = ""
        self.recall_index = len(self.recalled)

    def action_complete(self) -> None:
        # Input keeps its suggestion private; accepting it is what the right arrow does at the end of the line.
        if self._suggestion and self.cursor_at_end:
            self.action_cursor_right()
        else:
            self.screen.focus_next()


class MessageLog(RichLog):
    """Command output and the job's log, each entry under the local time it was written.

    A blank line is added before a block (``block``: a command echo, the job's result) and before and after a message
    longer than one line, so each command's output and each run read apart. Two blank lines never follow each other, and
    none is added at the top, or right after ``clear``.
    """

    def __init__(self, *args: Any, **options: Any) -> None:
        super().__init__(*args, **options)
        # Whether the last line written is blank (the top counts as one), and whether the last message was a block.
        self.blank_last = True
        self.after_block = False

    def say(self, text: Text | str, style: str = "", *, block: bool = False) -> None:
        # Text, never markup: job files, prompts, and errors contain brackets.
        body = text.copy() if isinstance(text, Text) else Text(text, style=style)
        # The spacing is added here, so a message's own trailing newlines would only double it.
        body.rstrip()
        several = "\n" in body.plain
        if (block or several or self.after_block) and not self.blank_last:
            self.write(Text(""))
        line = Text(datetime.now().strftime("%H:%M:%S "), style="dim")
        line.append_text(body)
        self.write(line)
        self.blank_last = False
        self.after_block = several

    def clear(self) -> Self:
        self.blank_last = True
        self.after_block = False
        return super().clear()
