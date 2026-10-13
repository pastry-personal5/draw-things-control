"""The executions a queue entry's resume chain reads: the one definition a resume (``queue_resume._resolve_chain``)
and a deletion's in-use refusal and resume warning (``history_delete.py``, Milestone 06) share."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Protocol, TypeVar


class ChainEntry(Protocol):
    """What the walk reads of an entry: the queue number it resumes."""

    @property
    def resumes(self) -> int | None: ...


E = TypeVar("E", bound=ChainEntry)
X = TypeVar("X")


def walk_chain(entry: E, *, linked: Callable[[E], X | None], ancestor: Callable[[E], E], succeeded: Callable[[X], bool]) -> Iterator[tuple[E, X | None]]:
    """Each entry of ``entry``'s chain with its linked execution (None when it has none), from ``entry`` itself back
    through the entries it resumes, ending with the first whose execution has a succeeded run, or with the chain's
    first entry. ``linked`` and ``ancestor`` (called with an entry that resumes another) may raise to end the walk:
    a resume refuses there, naming the reason."""
    current = entry
    while True:
        execution = linked(current)
        yield current, execution
        if (execution is not None and succeeded(execution)) or current.resumes is None:
            return
        current = ancestor(current)
