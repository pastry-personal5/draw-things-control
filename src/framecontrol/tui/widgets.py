"""The TUI's small widgets: the command field, the message log, and the text the one-second tick sets."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Self

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.visual import VisualType
from textual.widgets import Input, RichLog, Static

from framecontrol.tui.commands import CommandSuggester
from framecontrol.tui.shortcut_help import ShortcutGroups, refresh_status_line

MAX_MESSAGE_LINES = 5000
COMMAND_FIELD_SHORTCUTS: ShortcutGroups = (
    (
        "Command field",
        (
            ("Enter", "Run the command"),
            ("Tab", "Complete; again to step through the choices"),
            ("Up/Down", "Recall earlier commands, kept across sessions"),
            ("Escape", "Clear the command field"),
        ),
    ),
    (
        "Scrolling",
        (
            ("PageUp/PageDown", "Scroll the Activity log"),
            ("Shift-PageUp/PageDown", "Scroll the generation output"),
        ),
    ),
    (
        "General",
        (
            ("Tab (nothing to complete)", "Move focus to the Jobs list"),
            ("Ctrl-C", "Clear it, or press twice to quit"),
            ("F1", "Show shortcut help"),
        ),
    ),
)


class SteadyText(Static):
    """Text of a fixed size (``styles.tcss`` sets its height) that the one-second tick sets again whether or not it
    changed: the work status, the run summary, and the status line. An unchanged text is not drawn again, and a changed
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


class CommandField(Input):
    """The command field: a steady cursor, Tab completion, Escape to clear, Up/Down to recall commands, and PageUp/PageDown
    to scroll the logs (the logs take no focus, so this is how the keyboard reaches them)."""

    BINDINGS = [
        Binding("tab", "complete", "Complete", show=False),
        Binding("up", "recall(-1)", "Earlier command", show=False),
        Binding("down", "recall(1)", "Later command", show=False),
        Binding("escape", "clear_line", "Clear", show=False),
        Binding("pageup", "scroll_log('#activity-log', -1)", "Activity log up", show=False),
        Binding("pagedown", "scroll_log('#activity-log', 1)", "Activity log down", show=False),
        Binding("shift+pageup", "scroll_log('#output-log', -1)", "Output log up", show=False),
        Binding("shift+pagedown", "scroll_log('#output-log', 1)", "Output log down", show=False),
    ]

    def __init__(self, *, history: list[str] | None = None, **options: Any) -> None:
        super().__init__(**options)
        self.cursor_blink = False
        # Commands entered, oldest first: those of earlier sessions (``history``), then this session's.
        self.recalled: list[str] = list(history or [])
        # The position in ``recalled`` shown now; its length means the line being typed.
        self.recall_index = len(self.recalled)
        # While Tab cycles through several completions: the choices, the one shown, and the text it put in the field.
        self.cycle: list[str] = []
        self.cycle_index = 0
        self.cycle_shown = ""

    def on_focus(self, event: events.Focus) -> None:
        refresh_status_line(self)

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

    def action_scroll_log(self, selector: str, direction: int) -> None:
        log = self.screen.query_one(selector, RichLog)
        if direction < 0:
            log.scroll_page_up(animate=False)
        else:
            log.scroll_page_down(animate=False)

    def action_clear_line(self) -> None:
        self.value = ""
        self.recall_index = len(self.recalled)

    def action_complete(self) -> None:
        """Tab: finish the line when it has one completion; with several, add what they share, then step through them (Tab again);
        with none, move the focus on."""
        if self.cycle and self.value == self.cycle_shown:
            self.cycle_index = (self.cycle_index + 1) % len(self.cycle)
            self.show_completion()
            return
        suggester = self.suggester
        found = suggester.candidates(self.value) if self.value and self.cursor_at_end and isinstance(suggester, CommandSuggester) else []
        if not found:
            self.screen.focus_next()
        elif len(found) == 1:
            self.put(found[0])
        elif len(os.path.commonprefix(found)) > len(self.value):
            self.put(os.path.commonprefix(found))
        else:
            self.cycle, self.cycle_index = found, 0
            self.show_completion()

    def put(self, text: str) -> None:
        self.cycle = []
        self.value = text
        self.cursor_position = len(text)
        self.report_completions()

    def show_completion(self) -> None:
        self.cycle_shown = self.cycle[self.cycle_index]
        self.value = self.cycle_shown
        self.cursor_position = len(self.value)
        self.report_completions()

    def on_input_changed(self, event: Input.Changed) -> None:
        if self.cycle and event.value != self.cycle_shown:
            self.cycle = []
            self.report_completions()

    def report_completions(self) -> None:
        """Tell the status line which completions Tab is stepping through, and which is in the field (none: it shows its usual text)."""
        callback = getattr(self.screen, "show_completions", None)
        if callable(callback):
            callback(self.cycle, self.cycle_index)


class ActivityLog(RichLog):
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
