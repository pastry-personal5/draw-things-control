"""The ``X-Dtc-Caller`` header: which front end made a request, for the audit log's caller column and the worker's
busy message. Restricted to a known set (owner decision, phase-3-changelog.md), so the column stays clean; the
caller still only names itself, so this informs rather than proves."""

from __future__ import annotations

from draw_things_control.core.errors import InputError

HEADER_NAME = "X-Dtc-Caller"
KNOWN_CALLERS = frozenset({"cli", "tui", "mcp"})
DEFAULT_CALLER = "api"


def resolve_caller(header_value: str | None) -> str:
    """``header_value`` (the header's value, or None when absent) as the caller name the audit log stores. Absent
    defaults to ``api``; any other value is refused with ``invalid_input`` naming the field."""
    if header_value is None:
        return DEFAULT_CALLER
    if header_value not in KNOWN_CALLERS:
        raise InputError(f"'{HEADER_NAME}' must be one of {', '.join(sorted(KNOWN_CALLERS))}", field=HEADER_NAME)
    return header_value
