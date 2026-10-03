"""The TUI must refuse an ID before it becomes an HTTP path segment."""

from __future__ import annotations

import unittest
from pathlib import Path

from draw_things_control.tui.queue_client import ApiError, cancel, park, resume, show, unpark


class QueueIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_path_separator_never_reaches_the_http_client(self) -> None:
        for action in (show, cancel, resume, park, unpark):
            with self.subTest(action=action.__name__), self.assertRaises(ApiError) as caught:
                await action("http://127.0.0.1:8765", Path("no-token-file"), None, "Q0001/watch")
            self.assertIn("queue_id", caught.exception.text)
