"""Tests for core/client_config.py: reading a client's bearer token, and its --allow-remote-server rule."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from draw_things_control.core.client_config import check_server_host, read_client_token
from draw_things_control.core.errors import InputError, StateUnavailableError


class ReadClientTokenTests(unittest.TestCase):
    def test_reads_and_strips_the_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("a-token\n", encoding="ascii")
            self.assertEqual(read_client_token(path), "a-token")

    def test_a_missing_file_is_state_unavailable_naming_dtc_serve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing"
            with self.assertRaises(StateUnavailableError) as caught:
                read_client_token(path)
            self.assertIn("dtc serve", str(caught.exception))

    def test_an_empty_file_is_state_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("", encoding="ascii")
            with self.assertRaises(StateUnavailableError):
                read_client_token(path)


class CheckServerHostTests(unittest.TestCase):
    def test_a_loopback_url_is_accepted(self) -> None:
        check_server_host("http://127.0.0.1:8765", allow_remote_server=False)
        check_server_host("http://localhost:8765", allow_remote_server=False)

    def test_a_remote_url_is_refused_without_allow_remote_server(self) -> None:
        with self.assertRaises(InputError):
            check_server_host("http://example.com:8765", allow_remote_server=False)

    def test_a_remote_url_is_accepted_with_allow_remote_server(self) -> None:
        check_server_host("http://example.com:8765", allow_remote_server=True)
