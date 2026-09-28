"""Tests for the server's bearer token file: generation, permission checks, and constant-time comparison."""

import os
import tempfile
import unittest
from pathlib import Path

from draw_things_control.server.token_file import TOKEN_BYTES, TokenFileError, load_or_create_token, token_matches


class TokenFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.path = Path(self._temporary.name) / "config" / "server-token"

    def test_a_missing_file_is_created_with_mode_0600_and_a_64_character_hex_token(self) -> None:
        token = load_or_create_token(self.path)
        self.assertEqual(len(token), TOKEN_BYTES * 2)
        int(token, 16)  # does not raise: every character is hex
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")

    def test_reading_again_returns_the_same_token(self) -> None:
        first = load_or_create_token(self.path)
        second = load_or_create_token(self.path)
        self.assertEqual(first, second)

    def test_a_file_readable_by_others_is_refused(self) -> None:
        load_or_create_token(self.path)
        self.path.chmod(0o644)
        with self.assertRaisesRegex(TokenFileError, "readable by others"):
            load_or_create_token(self.path)

    def test_a_file_writable_by_the_group_is_refused(self) -> None:
        load_or_create_token(self.path)
        self.path.chmod(0o660)
        with self.assertRaisesRegex(TokenFileError, "readable by others"):
            load_or_create_token(self.path)

    def test_token_matches_is_constant_time_and_rejects_none(self) -> None:
        self.assertTrue(token_matches("abc123", "abc123"))
        self.assertFalse(token_matches("abc123", "abc124"))
        self.assertFalse(token_matches("abc123", None))
        self.assertFalse(token_matches("abc123", ""))

    def test_a_non_ascii_presented_token_is_a_mismatch_not_an_error(self) -> None:
        self.assertFalse(token_matches("abc123", "abc\xff23"))
        self.assertFalse(token_matches("abc123", "\udcff"))

    def test_an_empty_token_never_matches(self) -> None:
        self.assertFalse(token_matches("", ""))

    def test_an_empty_token_file_is_refused(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("\n", encoding="ascii")
        self.path.chmod(0o600)
        with self.assertRaisesRegex(TokenFileError, "empty"):
            load_or_create_token(self.path)

    def test_a_failed_write_leaves_no_token_file_behind(self) -> None:
        real_write = os.write

        def failing_write(descriptor: int, data: bytes) -> int:
            raise OSError(28, "No space left on device")

        os.write = failing_write  # type: ignore[assignment]
        try:
            with self.assertRaisesRegex(TokenFileError, "Cannot write"):
                load_or_create_token(self.path)
        finally:
            os.write = real_write  # type: ignore[assignment]
        self.assertFalse(self.path.exists())

    def test_a_directory_that_cannot_be_created_is_reported(self) -> None:
        blocked = Path(self._temporary.name) / "blocked"
        blocked.write_text("not a directory")
        with self.assertRaises(TokenFileError):
            load_or_create_token(blocked / "server-token")

    def test_owned_by_another_user_is_refused(self) -> None:
        load_or_create_token(self.path)
        real_getuid = os.getuid
        try:
            os.getuid = lambda: real_getuid() + 1  # type: ignore[method-assign]
            with self.assertRaisesRegex(TokenFileError, "owned by another user"):
                load_or_create_token(self.path)
        finally:
            os.getuid = real_getuid  # type: ignore[method-assign]


if __name__ == "__main__":
    unittest.main()
