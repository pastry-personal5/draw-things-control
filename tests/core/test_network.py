"""Tests for core/network.py: loopback hostnames and gRPC target bracketing, shared by every front end that binds
or dials one (a server's own bind check, and a client's --allow-remote-server refusal, Milestone 03)."""

from __future__ import annotations

import unittest

from draw_things_control.core.network import grpc_target, is_loopback_host


class LoopbackTests(unittest.TestCase):
    def test_loopback_names_are_recognized_case_insensitively(self) -> None:
        for host in ("127.0.0.1", "::1", "localhost", "LOCALHOST"):
            self.assertTrue(is_loopback_host(host))

    def test_a_remote_host_is_not_loopback(self) -> None:
        self.assertFalse(is_loopback_host("192.168.1.5"))


class GrpcTargetTests(unittest.TestCase):
    """An IPv6 host must be bracketed for a gRPC channel or bind: unbracketed, ``::1:8766`` reads as three
    colon-separated fields, not one host and one port."""

    def test_an_ipv4_host_is_not_bracketed(self) -> None:
        self.assertEqual(grpc_target("127.0.0.1", 8766), "127.0.0.1:8766")

    def test_a_hostname_is_not_bracketed(self) -> None:
        self.assertEqual(grpc_target("localhost", 8766), "localhost:8766")

    def test_an_ipv6_host_is_bracketed(self) -> None:
        self.assertEqual(grpc_target("::1", 8766), "[::1]:8766")

    def test_an_ipv6_wildcard_host_is_bracketed(self) -> None:
        self.assertEqual(grpc_target("::", 8766), "[::]:8766")
