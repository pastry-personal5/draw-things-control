"""Tests for the Host header check that keeps a browser out through DNS rebinding."""

import unittest

from draw_things_control.server.host_check import host_header_is_allowed, is_loopback_host


class IsLoopbackHostTests(unittest.TestCase):
    def test_loopback_names_are_recognized_in_any_case(self) -> None:
        for host in ("127.0.0.1", "::1", "localhost", "LOCALHOST"):
            with self.subTest(host):
                self.assertTrue(is_loopback_host(host))

    def test_a_routable_address_is_not_loopback(self) -> None:
        for host in ("0.0.0.0", "192.168.1.5", "example.com"):
            with self.subTest(host):
                self.assertFalse(is_loopback_host(host))


class HostHeaderIsAllowedTests(unittest.TestCase):
    def test_loopback_headers_are_allowed_regardless_of_the_bound_address(self) -> None:
        for header in ("127.0.0.1:8765", "127.0.0.1", "localhost:8765", "[::1]:8765"):
            with self.subTest(header):
                self.assertTrue(host_header_is_allowed(header, bound_host="192.168.1.5", bound_port=8765))

    def test_the_bound_address_and_port_are_allowed(self) -> None:
        self.assertTrue(host_header_is_allowed("192.168.1.5:8765", bound_host="192.168.1.5", bound_port=8765))

    def test_the_bound_address_without_a_port_is_allowed(self) -> None:
        self.assertTrue(host_header_is_allowed("192.168.1.5", bound_host="192.168.1.5", bound_port=8765))

    def test_a_mismatched_port_on_the_bound_address_is_refused(self) -> None:
        self.assertFalse(host_header_is_allowed("192.168.1.5:9999", bound_host="192.168.1.5", bound_port=8765))

    def test_an_unrelated_host_is_refused(self) -> None:
        self.assertFalse(host_header_is_allowed("evil.example.com:8765", bound_host="192.168.1.5", bound_port=8765))

    def test_a_missing_or_empty_header_is_refused(self) -> None:
        self.assertFalse(host_header_is_allowed(None, bound_host="127.0.0.1", bound_port=8765))
        self.assertFalse(host_header_is_allowed("", bound_host="127.0.0.1", bound_port=8765))

    def test_a_malformed_header_is_refused_not_raised(self) -> None:
        self.assertFalse(host_header_is_allowed("::not-a-valid-host::", bound_host="127.0.0.1", bound_port=8765))


if __name__ == "__main__":
    unittest.main()
