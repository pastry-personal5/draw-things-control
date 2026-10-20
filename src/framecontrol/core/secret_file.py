"""Private bearer-token files shared by the API and the MCP listener."""

from __future__ import annotations

import errno
import hmac
import os
import re
import secrets
import stat
from pathlib import Path

from framecontrol.core.errors import StateUnavailableError

TOKEN_BYTES = 32


class TokenFileError(StateUnavailableError):
    """A token file cannot be used: unsafe permissions, wrong owner, or unreadable."""


def load_or_create_token(path: Path, *, label: str = "server token") -> str:
    """Read or create a 32-byte hex token at ``path``. Refuse unsafe existing files.

    To rotate, stop the process using the token, remove its file, and restart it.
    """
    if path.exists():
        return _load_existing(path, label)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise TokenFileError(f"Cannot create {path.parent}: {error.strerror}") from error
    token = secrets.token_hex(TOKEN_BYTES)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another process created it between the exists() check and here.
        return _load_existing(path, label)
    except OSError as error:
        raise TokenFileError(f"Cannot create the {label} file {path}: {error.strerror}") from error
    data = token.encode("ascii")
    try:
        try:
            if os.write(descriptor, data) != len(data):
                raise OSError(errno.EIO, "short write")
        finally:
            os.close(descriptor)
    except OSError as error:
        # Never leave an empty or partial token file behind for the next start to load.
        path.unlink(missing_ok=True)
        raise TokenFileError(f"Cannot write the {label} file {path}: {error.strerror}") from error
    return token


def _load_existing(path: Path, label: str) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as error:
        raise TokenFileError(f"Cannot read the {label} file {path}: {error.strerror}") from error
    try:
        with os.fdopen(descriptor, "rb") as file:
            status = os.fstat(file.fileno())
            if not stat.S_ISREG(status.st_mode):
                raise TokenFileError(f"The {label} file {path} is not a regular file")
            if status.st_mode & 0o077:
                raise TokenFileError(f"The {label} file {path} is readable by others (mode {oct(status.st_mode & 0o777)}); chmod it to 600 or delete it to generate a new one")
            if status.st_uid != os.getuid():
                raise TokenFileError(f"The {label} file {path} is owned by another user; delete it to generate a new one")
            raw = file.read(TOKEN_BYTES * 2 + 2)
        token = raw.decode("ascii").strip()
    except (OSError, UnicodeDecodeError) as error:
        raise TokenFileError(f"Cannot read the {label} file {path}: {getattr(error, 'strerror', None) or error}") from error
    if not token:
        # An empty token would match an empty bearer value: refused, never served with.
        raise TokenFileError(f"The {label} file {path} is empty; delete it to generate a new one")
    if len(raw) > TOKEN_BYTES * 2 + 1 or re.fullmatch(r"[0-9a-fA-F]{64}", token) is None:
        raise TokenFileError(f"The {label} file {path} must contain a 64-digit hexadecimal token; delete it to generate a new one")
    return token


def token_matches(token: str, presented: str | None) -> bool:
    """Whether ``presented`` (an ``Authorization`` header's value after ``Bearer ``) equals ``token``, compared in
    constant time so a wrong guess cannot be timed. Compared as bytes: ``hmac.compare_digest`` refuses a ``str``
    with non-ASCII characters (a header byte above 0x7f, decoded as Latin-1) with a TypeError, not a mismatch."""
    return bool(token) and presented is not None and hmac.compare_digest(token.encode("utf-8"), presented.encode("utf-8", "surrogatepass"))
