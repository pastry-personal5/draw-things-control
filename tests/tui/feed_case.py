"""A base for TUI tests that drive the draw-things-cli pane through the gRPC feed, with a fake ``dtc serve``."""

from __future__ import annotations

from typing import Any

from draw_things_control.jobs.events import JobEvent
from draw_things_control.tui.app import DrawThingsApp
from draw_things_control.tui.generated import monitor_pb2
from tests.tui.tui_case import TuiTestCase


class FeedTestCase(TuiTestCase):
    async def connect(self, pilot: Any) -> DrawThingsApp:
        """Wait for the feed's first ``Reset`` to be handled: from then on the fake server's events reach the app."""
        app = pilot.app
        await self.wait_for(pilot, lambda: app.feed_connected, "the feed to connect")
        await self.settle(pilot)
        return app

    async def push(self, pilot: Any, *events: JobEvent) -> None:
        """Send the events and wait until the app has applied them."""
        sent = [self.server.send(event) for event in events]
        await self.received(pilot, sent)

    async def received(self, pilot: Any, sent: list[monitor_pb2.Event]) -> None:
        """Wait until the feed has received the last of ``sent`` that its open call asked for (none, at verbose low, for run output)."""
        app = pilot.app
        request = self.server.open_calls[-1].request
        wanted = [event.id for event in sent if request.include_output or event.kind != "run_output"]
        if wanted:
            await self.wait_for(pilot, lambda: app._feed.last_event_id >= max(wanted), "the events to be received")
        await pilot.pause()
        await pilot.pause()

    def log(self) -> str:
        return "\n".join(self.said)
