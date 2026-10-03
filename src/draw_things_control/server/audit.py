"""Wraps a queue (and, from Milestone 07, a write) action with one ``audit_log`` row: the outcome (``ok``, or the
error code) and the caller, whether the action was accepted or refused. Built with the first endpoints that accept
input, so no submission or write goes unrecorded (Milestone 02)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from draw_things_control.core.clock import Clock, local_timestamp
from draw_things_control.core.errors import DtcError
from draw_things_control.state.store import Store


@dataclass
class AuditTarget:
    value: str | None = None


@contextmanager
def audited(store: Store, *, action: str, target: str | None, caller: str, clock: Clock = datetime.now) -> Iterator[AuditTarget]:
    """Record one row when the block exits: ``ok``, a raised ``DtcError``'s code, or ``internal_error`` for an
    unexpected failure. The yielded target can be set once a reference resolves."""
    known = AuditTarget(target)
    try:
        yield known
    except DtcError as error:
        store.audit.record(action=action, target=known.value, outcome=error.code, caller=caller, at=local_timestamp(clock()))
        raise
    except Exception:
        store.audit.record(action=action, target=known.value, outcome="internal_error", caller=caller, at=local_timestamp(clock()))
        raise
    else:
        store.audit.record(action=action, target=known.value, outcome="ok", caller=caller, at=local_timestamp(clock()))
