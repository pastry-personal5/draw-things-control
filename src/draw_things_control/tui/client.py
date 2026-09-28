"""What the TUI needs to reach ``dtc serve``: the gRPC stub Protocol the feed worker and the Queue widget's client
use (Milestone 03), mirroring ``cli/queue_wait.py``'s own. Each front end holds its own copy of the generated
stubs (``cli/generated/``, ``tui/generated/``, ``server/generated/``); nothing here imports another's."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Protocol

from draw_things_control.tui.generated import monitor_pb2

CALLER_HEADER = "X-Dtc-Caller"
CALLER = "tui"


class WatchEventsCall(Protocol):
    def __aiter__(self) -> AsyncIterator[monitor_pb2.Event]: ...
    def cancel(self) -> None: ...


class MonitorStub(Protocol):
    """What the feed worker needs from a gRPC stub: only ``WatchEvents``, real or faked (a test's own
    ``grpc_stub_factory`` seam, the same shape ``cli/queue_wait.py``'s own takes)."""

    def WatchEvents(self, request: monitor_pb2.WatchEventsRequest, metadata: tuple[tuple[str, str], ...] | None = None) -> WatchEventsCall: ...


GrpcStubFactory = Callable[[str], object]
