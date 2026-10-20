"""Keeps a browser out through DNS rebinding: a bind beyond loopback is the owner's explicit choice
(``--allow-remote-bind``), and every HTTP request's ``Host`` header must name loopback or the bound address, since a
malicious page can make the browser resolve any name to 127.0.0.1 but cannot forge the ``Host`` header it sends.
gRPC has no equivalent gap: a channel dials the bound host and port directly, with no
virtual-hosting header to fake."""

from __future__ import annotations

from urllib.parse import urlsplit

from framecontrol.core.network import LOOPBACK_HOSTNAMES, is_loopback_host

__all__ = ["LOOPBACK_HOSTNAMES", "is_loopback_host", "host_header_is_allowed"]

# --host values that bind every interface at once, not one address of their own: no client's Host header can ever
# equal one of these literally, so a literal match against bound_host would refuse every real remote request.
WILDCARD_HOSTNAMES = frozenset({"0.0.0.0", "::"})


def host_header_is_allowed(host_header: str | None, *, bound_host: str, bound_port: int) -> bool:
    """Whether ``host_header`` (an HTTP request's ``Host`` header) names loopback or the address the server is
    actually bound to, port included when the header gives one. When ``bound_host`` is a wildcard bind
    (``0.0.0.0`` or ``::``, only reachable with ``--allow-remote-bind``), any hostname is accepted: the owner
    already chose to expose every interface, and no Host header can name the wildcard address itself."""
    # Host is an authority, not a URL. urlsplit alone also accepts a userinfo prefix,
    # path, query, or fragment and silently strips some control characters.
    if not host_header or any(character in host_header for character in "/?#@\\%") or any(ord(character) <= 32 or ord(character) >= 127 for character in host_header):
        return False
    try:
        split = urlsplit(f"//{host_header}")
        hostname, port = split.hostname, split.port
    except ValueError:
        return False
    if hostname is None or split.netloc != host_header or host_header.endswith(":"):
        return False
    if hostname in LOOPBACK_HOSTNAMES:
        return port is None or 0 < port <= 65535
    if bound_host.lower() in WILDCARD_HOSTNAMES:
        return port is None or port == bound_port
    return hostname == bound_host.lower() and (port is None or port == bound_port)
