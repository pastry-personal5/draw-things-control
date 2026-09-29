"""The state store: one SQLite database of job executions and their runs, shared by every front end."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from loguru import logger

from draw_things_control.core.clock import Clock
from draw_things_control.core.paths import DATABASE_FILE_NAME
from draw_things_control.core.run_lock import ensure_state_directory
from draw_things_control.state.audit import AuditRepository
from draw_things_control.state.database import Database, StateError
from draw_things_control.state.executions import DeletedExecution, ExecutionRepository
from draw_things_control.state.job_ids import JobIdRepository
from draw_things_control.state.queue import QueueRepository
from draw_things_control.state.schema import SCHEMA_VERSION
from draw_things_control.state.settings import SettingsRepository

SECONDS_PER_DAY = 86400
DEFAULT_RETENTION_DAYS = 14

__all__ = ["DATABASE_FILE_NAME", "SCHEMA_VERSION", "StateError", "Store", "StoreMode"]


class StoreMode(StrEnum):
    """How a store is opened. Every mode upgrades an older database (an upgrade is the one write a read-only screen may make)."""

    # Creates the database, and prunes old history: a job's recording.
    RUN = "run"
    # Creates the database, and never prunes: keeping job IDs and settings.
    WRITE = "write"
    # Never creates the database, and never prunes: reading history.
    BROWSE = "browse"


def _delete_log(path: Path) -> None:
    """Delete a pruned execution's log file. Only a regular ``.log`` file goes, and a file already gone or refused is no error: the row is deleted either way."""
    if path.suffix != ".log":
        return
    # is_symlink() is inside the try: it raises too when the directory cannot be searched.
    try:
        if not path.is_symlink():
            path.unlink(missing_ok=True)
    except OSError as error:
        logger.warning("Could not delete the old log {}: {}", path, error.strerror)


def _delete_manifest(path: Path) -> bool:
    """Delete a deleted execution's manifest (Milestone 06), so ``dtc import-history`` cannot bring it back; returns
    whether it is gone. Only a regular ``.json`` file goes: a symbolic link or another file is kept, and so is one that
    cannot be deleted (logged), since the importer could still find it. A file already gone is no error."""
    # is_symlink() and exists() are inside the try: they raise too when the directory cannot be searched.
    try:
        if path.suffix != ".json" or path.is_symlink():
            return not path.is_symlink() and not path.exists()
        path.unlink(missing_ok=True)
    except OSError as error:
        logger.warning("Could not delete the manifest {}: {}", path, error.strerror)
        return False
    return True


class Store:
    """The state database, with a repository for each kind of data."""

    def __init__(self, database: Database, *, retention_days: int = DEFAULT_RETENTION_DAYS, clock: Clock = datetime.now) -> None:
        self._database = database
        self._retention_days = retention_days
        self._clock = clock
        self.executions = ExecutionRepository(database)
        self.job_ids = JobIdRepository(database)
        self.settings = SettingsRepository(database)
        self.queue = QueueRepository(database)
        self.audit = AuditRepository(database)

    @classmethod
    def open(cls, path: Path, *, mode: StoreMode = StoreMode.RUN, retention_days: int = DEFAULT_RETENTION_DAYS, clock: Clock = datetime.now) -> Store:
        """Open the database at ``path`` in ``mode``, creating its directory when the mode creates the database; raises StateError when it cannot be used."""
        if mode is not StoreMode.BROWSE:
            ensure_state_directory(path.parent)
        database = Database(path, create=mode is not StoreMode.BROWSE)
        store = cls(database, retention_days=retention_days, clock=clock)
        if mode is StoreMode.RUN:
            try:
                store.prune()
            except BaseException:
                # The caller gets no Store to close, so close here.
                store.close()
                raise
        return store

    @property
    def path(self) -> Path:
        return self._database.path

    def close(self) -> None:
        """Close every thread's connection."""
        self._database.close()

    def sweep_interrupted(self) -> int:
        """Close every ``running`` execution as ``interrupted``. Only call this while holding the run lock (see ``ExecutionRepository.sweep_interrupted``)."""
        return self.executions.sweep_interrupted(self._clock())

    def prune(self) -> int:
        """Delete executions (and their runs), and finished queue entries, before the retention cutoff; never a
        ``running`` execution or a ``queued`` or ``running`` queue entry, nor a parked entry and the chain of resumes
        below it, or their executions, until one of those resumes has a succeeded run (Milestone 05). An execution's
        ``.log`` file is deleted too, never its manifest or outputs."""
        cutoff = self.retention_cutoff()
        if cutoff is None:
            return 0
        # Read before either prune, so both keep the same entries.
        kept = self.queue.kept_parked()
        deleted, log_paths = self.executions.prune(cutoff, keep=[execution for _queue, execution in kept if execution is not None])
        for log_path in log_paths:
            _delete_log(Path(log_path))
        return deleted + self.queue.prune(cutoff, keep=[queue for queue, _execution in kept])

    def delete_execution_files(self, deleted: Sequence[DeletedExecution]) -> list[tuple[int, str]]:
        """Delete the log and manifest of each execution ``ExecutionRepository.delete`` deleted, once its transaction
        committed; returns the manifests that stayed, as (execution number, path). A file that cannot be deleted is
        logged, and the row stays deleted either way."""
        kept: list[tuple[int, str]] = []
        for execution in deleted:
            if execution.log_path is not None:
                _delete_log(Path(execution.log_path))
            if execution.manifest_path is not None and not _delete_manifest(Path(execution.manifest_path)):
                kept.append((execution.number, execution.manifest_path))
        return kept

    def retention_cutoff(self) -> float | None:
        """The UTC epoch before which history is pruned, or None when it is kept forever."""
        return self._clock().timestamp() - self._retention_days * SECONDS_PER_DAY if self._retention_days > 0 else None
