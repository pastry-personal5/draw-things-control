"""The SQLite file behind the state store: its permissions, one connection per thread, migrations, and transactions."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from draw_things_control.core.errors import StateUnavailableError
from draw_things_control.state.schema import MIGRATIONS, SCHEMA_VERSION

BUSY_TIMEOUT_MILLISECONDS = 5000


class StateError(StateUnavailableError):
    """The state database cannot be used: a newer schema, no WAL support, or a file that will not open."""


class Database:
    """One SQLite database file. One connection per thread, since sqlite3 connections are not thread-safe.

    Without ``create``, a file that does not exist is an error, not a new database.

    Opening migrates an older database forward, whoever opens it (an upgrade is the one write a read-only screen may make).
    """

    def __init__(self, path: Path, *, create: bool = True) -> None:
        self._path = path
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        if create:
            self._create_file()
        elif not path.exists():
            raise StateError(f"The state database {path} does not exist")
        try:
            self._migrate(self.connection())
        except BaseException:
            # The caller gets no Database to close, so close here.
            self.close()
            raise

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        """Close every thread's connection."""
        with self._connections_lock:
            connections, self._connections = self._connections, []
        for connection in connections:
            connection.close()
        self._local = threading.local()

    def connection(self) -> sqlite3.Connection:
        """This thread's connection, opened on first use."""
        connection: sqlite3.Connection | None = getattr(self._local, "connection", None)
        if connection is None:
            connection = self._connect()
            self._local.connection = connection
            with self._connections_lock:
                self._connections.append(connection)
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A write transaction: ``BEGIN IMMEDIATE`` takes the write lock up front; it commits, or rolls back when the block raises."""
        connection = self.connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            # A failed COMMIT leaves the transaction open, which would fail every later write on this thread.
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    def _create_file(self) -> None:
        """Create the database file with mode 0600 first, so SQLite's WAL files inherit it: history holds prompts."""
        try:
            os.close(os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600))
        except OSError as error:
            raise StateError(f"Cannot create the state database {self._path}: {error.strerror}") from error

    def _connect(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            # Autocommit; transactions are explicit, so BEGIN IMMEDIATE takes the write lock up front.
            connection = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MILLISECONDS}")
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            connection.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error as error:
            if connection is not None:
                connection.close()
            raise StateError(f"Cannot open the state database {self._path}: {error}") from error
        if str(mode).lower() != "wal":
            connection.close()
            raise StateError(f"The state directory {self._path.parent} does not support SQLite WAL mode (journal mode is '{mode}'); it may be on a network or unsupported filesystem")
        return connection

    def _migrate(self, connection: sqlite3.Connection) -> None:
        try:
            if connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
                return
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Read again under the write lock: another process may have migrated in between.
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise StateError(f"{self._path} was written by a newer version of this program (schema {version}, this one knows {SCHEMA_VERSION}); update the program")
                for target in range(version + 1, SCHEMA_VERSION + 1):
                    for statement in MIGRATIONS[target - 1].split(";\n"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {target}")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        except sqlite3.Error as error:
            raise StateError(f"Cannot prepare the state database {self._path}: {error}") from error


def next_number(connection: sqlite3.Connection, counter: str) -> int:
    """Take the next number of ``counter`` inside the caller's transaction: the counter only goes up, so a number is never given twice."""
    connection.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (counter,))
    return int(connection.execute("SELECT value FROM counters WHERE name = ?", (counter,)).fetchone()[0])
