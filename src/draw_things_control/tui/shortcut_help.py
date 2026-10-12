"""The shortcut help overlay and the small helpers its focused components use."""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Static

ShortcutRows = Sequence[tuple[str, str]]


class ShortcutHelpOverlay(ModalScreen[None]):
    """A centered, non-destructive reference for the shortcuts of one focused component."""

    BINDINGS = [
        Binding("question_mark", "close", "Close", show=False, priority=True),
        Binding("escape", "close", "Close", show=False, priority=True),
    ]

    def __init__(self, component: str, shortcuts: ShortcutRows) -> None:
        super().__init__()
        self.component = component
        self.shortcuts = tuple(shortcuts)

    def compose(self) -> ComposeResult:
        width = max((len(key) for key, _ in self.shortcuts), default=0)
        text = Text()
        for key, action in self.shortcuts:
            text.append(f"{key.ljust(width)}  ", style="bold")
            text.append(f"{action}\n")
        text.rstrip()
        with Vertical(id="shortcut-help"):
            yield Static(f"{self.component} shortcuts", id="shortcut-help-title")
            yield Static(text, id="shortcut-help-rows")
            yield Static("? or Escape closes", id="shortcut-help-dismiss")

    def action_close(self) -> None:
        self.dismiss()


def show_shortcut_help(widget: Widget, component: str, shortcuts: ShortcutRows) -> None:
    """Show shortcut help without making a component know the application type."""
    widget.app.push_screen(ShortcutHelpOverlay(component, shortcuts))


def refresh_status_line(widget: Widget) -> None:
    """Refresh the main screen's focus-sensitive status-line hint, when this widget belongs to it."""
    callback = getattr(widget.screen, "refresh_status_line", None)
    if callable(callback):
        callback()
