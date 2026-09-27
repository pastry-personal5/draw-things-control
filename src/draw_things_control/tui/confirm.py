"""The yes-or-no dialog over the current screen."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Static


class ConfirmScreen(ModalScreen[bool]):
    """A yes-or-no question over the current screen; ``purpose`` names it (``run``, ``stop``, ``quit``).

    With ``enter_confirms`` false, only ``y`` answers yes: Enter also submits the command that opened the dialog,
    so a second Enter, or a held key, must not answer it.
    """

    BINDINGS = [
        Binding("y", "answer(True)", "Yes"),
        Binding("enter", "enter", "Yes", show=False),
        Binding("n", "answer(False)", "No"),
        Binding("escape", "answer(False)", "No", show=False),
    ]

    def __init__(self, text: Text, *, purpose: str, enter_confirms: bool = True) -> None:
        super().__init__()
        self.text = text
        self.purpose = purpose
        self.enter_confirms = enter_confirms

    def compose(self) -> ComposeResult:
        yield Static(self.text, id="confirm")

    def action_enter(self) -> None:
        if self.enter_confirms:
            self.dismiss(True)

    def action_answer(self, answer: bool) -> None:
        self.dismiss(answer)
