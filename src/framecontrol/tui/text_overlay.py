"""The text viewer overlay: a job's or a queue entry's details, scrollable, in front of the main screen."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static


class TextOverlay(ModalScreen[None]):
    """Long details that would drown the Activity log, read with the arrow and page keys; it changes nothing."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=False, priority=True),
        Binding("enter", "close", "Close", show=False, priority=True),
        Binding("q", "close", "Close", show=False, priority=True),
    ]

    def __init__(self, title: str, body: Text) -> None:
        super().__init__()
        self.title_text = title
        self.body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="text-overlay"):
            # Text, not str: the title carries a job's file name, and a str is parsed as markup.
            yield Static(Text(self.title_text), id="text-overlay-title")
            with VerticalScroll(id="text-overlay-scroll"):
                yield Static(self.body, id="text-overlay-body")
            yield Static("Up/Down, PageUp/PageDown, Home/End scroll; Escape, Enter, or q closes", id="text-overlay-dismiss")

    def on_mount(self) -> None:
        self.query_one(VerticalScroll).focus()

    def action_close(self) -> None:
        self.dismiss()
