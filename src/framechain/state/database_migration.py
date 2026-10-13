"""One-time rename of the state database, including committed WAL transactions."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from framechain.core.run_lock import RunLock, RunLockBusyError
from framechain.state.database import StateError

LEGACY_DATABASE_FILE_NAME = "dtc.db"


def migrate_legacy_database(path: Path) -> None:
    """Back up the legacy database before any Framechain connection opens state."""
    if path.name != "framechain.db":
        return
    old = path.parent / LEGACY_DATABASE_FILE_NAME
    if not old.exists():
        return
    if path.exists():
        raise StateError(f"Both {old} and {path} exist. Stop all servers, inspect both databases, and move the unwanted one aside before retrying; Framechain will not choose one automatically.")
    try:
        with RunLock("migrate", directory=path.parent, retry_seconds=0):
            # Recheck after taking the lock: another starter may have migrated it.
            if not old.exists():
                return
            if path.exists():
                raise StateError(f"Both {old} and {path} exist. Stop all servers, inspect both databases, and move the unwanted one aside before retrying; Framechain will not choose one automatically.")
            _backup_and_verify(old, path)
            for suffix in ("", "-wal", "-shm", "-journal"):
                (path.parent / f"{old.name}{suffix}").unlink(missing_ok=True)
    except RunLockBusyError as error:
        raise StateError(f"Cannot migrate {old} while the run lock is held: {error}. Stop the running server or job, then retry.") from error
    except OSError as error:
        raise StateError(f"Cannot migrate {old} to {path}: {error}. Stop other processes and retry; if both files exist, inspect them and move the unwanted one aside.") from error


def _backup_and_verify(old: Path, new: Path) -> None:
    source: sqlite3.Connection | None = None
    target: sqlite3.Connection | None = None
    try:
        # Exclusive creation refuses to overwrite an independently created destination.
        descriptor = os.open(new, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        source = sqlite3.connect(f"{old.absolute().as_uri()}?mode=ro", uri=True)
        target = sqlite3.connect(new)
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise StateError(f"Backup of {old} failed integrity verification; both files were kept for recovery")
        if target.execute("PRAGMA user_version").fetchone()[0] != source.execute("PRAGMA user_version").fetchone()[0]:
            raise StateError(f"Backup of {old} has a different schema version; both files were kept for recovery")
    except sqlite3.Error as error:
        raise StateError(f"Cannot back up {old} to {new}: {error}. Both files were kept; inspect them before retrying.") from error
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
