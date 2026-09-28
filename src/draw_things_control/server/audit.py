"""Wraps a queue (and, from Milestone 07, a write) action with one ``audit_log`` row: the outcome (``ok``, or the
error code) and the caller, whether the action was accepted or refused. Built with the first endpoints that accept
input, so no submission or write goes unrecorded (Milestone 02)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from draw_things_control.core.clock import Clock, local_timestamp
from draw_things_control.core.errors import DtcError
from draw_things_control.state.store import Store


@contextmanager
def audited(store: Store, *, action: str, target: str | None, caller: str, clock: Clock = datetime.now) -> Iterator[None]:
    """Records one row when the block exits: ``ok`` if it completed, or a raised ``DtcError``'s code (re-raised
    either way). A non-``DtcError`` is not recorded here: it becomes an unhandled 500, outside the API's stable
    error shape, not this request's own named outcome."""
    try:
        yield
    except DtcError as error:
        store.audit.record(action=action, target=target, outcome=error.code, caller=caller, at=local_timestamp(clock()))
        raise
    else:
        store.audit.record(action=action, target=target, outcome="ok", caller=caller, at=local_timestamp(clock()))
