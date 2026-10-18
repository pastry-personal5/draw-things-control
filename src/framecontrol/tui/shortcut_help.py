"""The shortcut help overlay and the small helpers its focused components use."""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Static

ShortcutRows = Sequence[tuple[str, str]]
ShortcutGroups = Sequence[tuple[str, ShortcutRows]]

# The keys every list and detail shares: shown last, below the keys of the focused component itself.
GENERAL_SHORTCUTS: tuple[str, ShortcutRows] = (
    "General",
    (
        ("Tab / Shift-Tab", "Move focus"),
        ("Escape", "Focus the command field"),
        ("Ctrl-C", "Press twice to quit"),
        ("? / F1", "Show shortcut help"),
    ),
)


class ShortcutHelpOverlay(ModalScreen[None]):
    """A large, centered, non-destructive reference for the shortcuts of one focused component, in categories: the component's own first, the general ones last."""

    BINDINGS = [
        Binding("question_mark", "close", "Close", show=False, priority=True),
        Binding("escape", "close", "Close", show=False, priority=True),
    ]

    def __init__(self, component: str, shortcuts: ShortcutGroups) -> None:
        super().__init__()
        self.component = component
        self.shortcuts = tuple((category, tuple(rows)) for category, rows in shortcuts)

    def compose(self) -> ComposeResult:
        width = max((len(key) for _, rows in self.shortcuts for key, _ in rows), default=0)
        text = Text()
        for index, (category, rows) in enumerate(self.shortcuts):
            if index:
                text.append("\n")
            text.append(f"{category}\n", style="bold underline")
            for key, action in rows:
                text.append(f"  {key.ljust(width)}  ", style="bold")
                text.append(f"{action}\n")
        text.rstrip()
        with Vertical(id="shortcut-help"):
            yield Static(f"{self.component} shortcuts", id="shortcut-help-title")
            with VerticalScroll(id="shortcut-help-scroll"):
                yield Static(text, id="shortcut-help-rows")
            yield Static("? or Escape closes", id="shortcut-help-dismiss")

    def action_close(self) -> None:
        self.dismiss()


def show_shortcut_help(widget: Widget, component: str, shortcuts: ShortcutGroups) -> None:
    """Show shortcut help without making a component know the application type."""
    widget.app.push_screen(ShortcutHelpOverlay(component, shortcuts))


def refresh_status_line(widget: Widget) -> None:
    """Refresh the main screen's focus-sensitive status-line hint, when this widget belongs to it."""
    callback = getattr(widget.screen, "refresh_status_line", None)
    if callable(callback):
        callback()
