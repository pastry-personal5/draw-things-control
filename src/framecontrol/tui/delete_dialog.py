"""The delete dialog, for the TUI action that cannot be undone.

Its own module, not ``screens.py``: ``tui/deletion.py`` pushes it, and
``screens.py`` already imports the controller that imports ``deletion.py``.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class DeleteDialog(ModalScreen[str]):
    """Asks before an execution is deleted, the TUI's one action that cannot be undone. Dismissed with
    the choice: ``delete``, ``skip``, ``all`` (delete this one and every remaining one without asking again), or
    ``cancel``. One execution offers only Delete and Cancel. Cancel has the focus, so Enter, which opens a row everywhere
    else, never deletes by a double tap or a held key: only ``y`` or a click on Delete does."""

    BINDINGS = [
        Binding("y", "choose('delete')", "Delete", show=False),
        Binding("s", "choose('skip')", "Skip", show=False),
        Binding("a", "choose('all')", "Delete all", show=False),
        Binding("n", "choose('cancel')", "Cancel", show=False),
        Binding("escape", "choose('cancel')", "Cancel", show=False),
    ]

    def __init__(self, summary: str, *, place: tuple[int, int], resumes_ended: list[str], remaining_ending: int = 0) -> None:
        super().__init__()
        self.summary = summary
        # This execution's place among those asked about, and how many are asked about.
        self.place = place
        self.resumes_ended = resumes_ended
        # How many of the executions after this one end a resume: said before Delete all.
        self.remaining_ending = remaining_ending

    @property
    def several(self) -> bool:
        return self.place[1] > 1

    def compose(self) -> ComposeResult:
        index, total = self.place
        lines = [f"Delete {self.summary}?" + (f"  {index} of {total}" if self.several else ""), "Its log and manifest are deleted; its outputs stay."]
        lines += [f"{queue_id} can no longer be resumed." for queue_id in self.resumes_ended]
        if self.several and self.remaining_ending:
            lines.append(f"Delete all: {self.remaining_ending} of the {total - index} after this one end a resume.")
        with Vertical(id="delete-dialog"):
            yield Static(Text("\n".join(lines)), id="delete-text")
            with Horizontal(id="delete-buttons"):
                yield Button("Delete", id="delete", variant="error")
                if self.several:
                    yield Button("Skip", id="skip")
                    yield Button("Delete all", id="all", variant="warning")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_choose(event.button.id or "cancel")

    def action_choose(self, choice: str) -> None:
        if choice in ("skip", "all") and not self.several:
            return
        self.dismiss(choice)
