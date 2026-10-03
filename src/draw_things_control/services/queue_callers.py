"""Keeping agents off the queue entries and holds people made (Milestone 10). Each entry records the caller that
submitted or resumed it, and the hold the caller that made it; an agent (the caller ``mcp``) may cancel, park, unpark,
or resume only an entry an agent submitted, and release only a hold an agent made. A person's commands are never
limited. The caller names itself (``X-Dtc-Caller``), so this guards agents that use their tools; it adds no level of
access to the API."""

from __future__ import annotations

from draw_things_control.core.errors import NotPermittedError
from draw_things_control.state.queue import QueueRow

# server/caller.py's name for the MCP server, the one caller this limits.
AGENT_CALLER = "mcp"


def is_agent(caller: str | None) -> bool:
    """Whether ``caller`` is an agent's; anything else, an unknown or a missing caller included, is a person's."""
    return caller == AGENT_CALLER


def maker_text(caller: str | None) -> str:
    """Who made an entry or a hold, for a refusal: ``tui``, or, for one made before callers were recorded (or a hold
    whose saved setting cannot be read), that it counts as a person's."""
    return caller if caller is not None else "a person (made before callers were recorded)"


def check_entry_permitted(entry: QueueRow, caller: str | None, action: str) -> None:
    """Refuse (``NotPermittedError``) an agent's ``action`` (cancel, park, unpark, resume) on an entry a person
    submitted. An entry's submitter never changes, so this check needs no lock with the action."""
    if is_agent(caller) and not is_agent(entry.submitted_by):
        raise NotPermittedError(f"{entry.label} was submitted by {maker_text(entry.submitted_by)}; an agent may {action} only an entry an agent submitted")
