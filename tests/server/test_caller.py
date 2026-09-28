"""Tests for the X-Dtc-Caller header."""

import unittest

from draw_things_control.core.errors import InputError
from draw_things_control.server.caller import HEADER_NAME, resolve_caller


class ResolveCallerTests(unittest.TestCase):
    def test_a_missing_header_defaults_to_api(self) -> None:
        self.assertEqual(resolve_caller(None), "api")

    def test_each_known_caller_is_accepted(self) -> None:
        for caller in ("cli", "tui", "mcp"):
            with self.subTest(caller):
                self.assertEqual(resolve_caller(caller), caller)

    def test_an_unknown_caller_is_refused_naming_the_header(self) -> None:
        with self.assertRaises(InputError) as context:
            resolve_caller("browser")
        self.assertEqual(context.exception.field, HEADER_NAME)

    def test_api_itself_is_not_an_accepted_header_value(self) -> None:
        # 'api' is the default for an absent header, not something a caller sends explicitly.
        with self.assertRaises(InputError):
            resolve_caller("api")


if __name__ == "__main__":
    unittest.main()
