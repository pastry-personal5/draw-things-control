"""The API's bearer token: 32 random bytes, hex-encoded, generated on first ``dtc serve`` and kept in
``ProjectPaths.server_token`` (mode 0600). Checked with a constant-time comparison; never logged or returned."""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from draw_things_control.core.errors import StateUnavailableError

TOKEN_BYTES = 32


class TokenFileError(StateUnavailableError):
    """The server token file cannot be used: unsafe permissions, owned by another user, or unreadable."""


def load_or_create_token(path: Path) -> str:
    """The hex-encoded token at ``path``, generating and writing it (mode 0600) on first use. An existing file
    readable by others, or owned by someone else, is refused with a message, not silently fixed (owner decision).
    To change the token, delete the file and restart the server."""
    if path.exists():
        return _load_existing(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise TokenFileError(f"Cannot create {path.parent}: {error.strerror}") from error
    token = secrets.token_hex(TOKEN_BYTES)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another process created it between the exists() check and here.
        return _load_existing(path)
    except OSError as error:
        raise TokenFileError(f"Cannot create the server token file {path}: {error.strerror}") from error
    try:
        os.write(descriptor, token.encode("ascii"))
    finally:
        os.close(descriptor)
    return token


def _load_existing(path: Path) -> str:
    try:
        status = path.stat()
    except OSError as error:
        raise TokenFileError(f"Cannot read the server token file {path}: {error.strerror}") from error
    if status.st_mode & 0o077:
        raise TokenFileError(f"The server token file {path} is readable by others (mode {oct(status.st_mode & 0o777)}); chmod it to 600 or delete it to generate a new one")
    if status.st_uid != os.getuid():
        raise TokenFileError(f"The server token file {path} is owned by another user; delete it to generate a new one")
    try:
        return path.read_text(encoding="ascii").strip()
    except OSError as error:
        raise TokenFileError(f"Cannot read the server token file {path}: {error.strerror}") from error


def token_matches(token: str, presented: str | None) -> bool:
    """Whether ``presented`` (an ``Authorization`` header's value after ``Bearer ``) equals ``token``, compared in
    constant time so a wrong guess cannot be timed."""
    return presented is not None and hmac.compare_digest(token, presented)
