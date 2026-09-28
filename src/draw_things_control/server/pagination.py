"""Pagination for the list endpoints (``GET /jobs``, ``/executions``, ``/inputs``, ``/audit``): a server-chosen
opaque cursor a client passes back unmodified to get the next page and never constructs itself, so the scheme behind
it can change later without breaking a client (owner decision, phase-3-changelog.md). ``GET /queue`` and
``GET /capabilities`` are not paged: the queue is bounded by ``max_queued_jobs``."""

from __future__ import annotations

import base64

from draw_things_control.core.errors import InputError

# Default and maximum, matching services/history.py's existing PAGE_SIZE (the TUI's own history paging).
DEFAULT_LIMIT = 200
MAX_LIMIT = 200


def encode_cursor(offset: int) -> str:
    """The opaque cursor naming the page that starts at ``offset``."""
    return base64.urlsafe_b64encode(str(offset).encode("ascii")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    """The offset ``cursor`` names; 0 when there is none. Raises InputError for a cursor this server did not issue,
    so a client that hand-builds one gets a clear refusal instead of a silently wrong page."""
    if cursor is None:
        return 0
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        offset = int(base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii"))
    except (ValueError, UnicodeDecodeError) as error:
        raise InputError("'cursor' is not a cursor this server issued", field="cursor") from error
    if offset < 0:
        raise InputError("'cursor' is not a cursor this server issued", field="cursor")
    return offset


def clamp_limit(limit: int | None) -> int:
    """``limit``, defaulting to and capped at 200; raises InputError for one below 1."""
    if limit is None:
        return DEFAULT_LIMIT
    if limit < 1:
        raise InputError("'limit' must be at least 1", field="limit")
    return min(limit, MAX_LIMIT)


def next_cursor(offset: int, limit: int, returned: int) -> str | None:
    """The cursor for the next page, or None when this page was not full (so there is nothing more to page to)."""
    return encode_cursor(offset + returned) if returned == limit else None
