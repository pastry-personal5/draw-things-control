"""What every client of `dtc serve` needs before it can call it: reading the bearer token from --token-file, and
refusing a --server-url beyond loopback without --allow-remote-server (Milestone 03's own client-side rule,
mirroring `dtc serve`'s own --allow-remote-bind check). `cli/`, `tui/`, and (Milestone 10) `mcp_server/` each hold
their own thin httpx/gRPC client around this: the framework calls themselves cannot live here (core imports no
framework, tests/test_architecture.py), only what every one of those clients needs in common."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from draw_things_control.core.errors import InputError, StateUnavailableError
from draw_things_control.core.network import is_loopback_host
from draw_things_control.core.paths import ProjectPaths

DEFAULT_SERVER_URL = "http://127.0.0.1:8765"


def job_argument(job: str, paths: ProjectPaths) -> str:
    """``job`` as the API takes it (a job ID or a file name): unchanged, except a path directly in ``data/jobs/``,
    which becomes just its name -- what the API resolves job files by. The one rule both `dtc queue add` (`cli/
    queue_app.py`) and the TUI's `/apply` (`tui/queue_client.py`) use, since the API only ever reads its own
    ``data/jobs/``, not wherever ``--data-dir`` points the TUI's own Job Definition widget at."""
    path = Path(job).expanduser()
    try:
        is_direct_child = path.parent.resolve() == paths.jobs.resolve()
    except OSError:
        return job
    return path.name if is_direct_child else job


def read_client_token(path: Path) -> str:
    """The bearer token at ``path`` (``--token-file``); raises ``StateUnavailableError`` (exit 1), naming
    ``dtc serve``, when it cannot be read -- the server was never started, or was started with a different
    ``ProjectPaths``. Never creates one: only ``dtc serve`` itself does that."""
    try:
        token = path.read_text(encoding="ascii").strip()
    except OSError as error:
        raise StateUnavailableError(f"Cannot read the server token file {path}: {error.strerror}; is 'dtc serve' running?") from error
    if not token:
        raise StateUnavailableError(f"The server token file {path} is empty; is 'dtc serve' running?")
    return token


def check_server_host(server_url: str, *, allow_remote_server: bool) -> None:
    """Refuses (``InputError``, exit 2) a ``--server-url`` whose host is not loopback, unless
    ``--allow-remote-server`` is given: the client's side of the same rule ``dtc serve --allow-remote-bind``
    enforces on the server, since the bearer token would otherwise cross the network in plain HTTP either way."""
    if allow_remote_server:
        return
    hostname = urlsplit(server_url).hostname
    if hostname is None or not is_loopback_host(hostname):
        raise InputError(f"--server-url {server_url} is not loopback; pass --allow-remote-server to reach it (the token then crosses the network in plain HTTP)")
