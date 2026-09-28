"""Loopback hostnames: whether a URL or a bind address names the machine itself, for every front end that has to
decide (a server refusing to bind beyond loopback without ``--allow-remote-bind``, or a client refusing to reach
beyond it without ``--allow-remote-server``, Milestone 03) without duplicating the same three names."""

from __future__ import annotations

LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "::1", "localhost"})


def is_loopback_host(host: str) -> bool:
    """Whether ``host`` (a bind address or a client target's hostname) names loopback."""
    return host.lower() in LOOPBACK_HOSTNAMES


def grpc_target(host: str, port: int) -> str:
    """``host:port`` for a gRPC channel or server bind, IPv6 literals bracketed: ``::1:8766`` reads as three
    colon-separated fields, not one host and one port, so an unbracketed ``--host ::1`` would either fail to parse
    or bind (or dial) the wrong address; ``[::1]:8766`` is unambiguous."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
