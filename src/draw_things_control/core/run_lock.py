"""A machine-wide lock that keeps two processes from driving the GPU at once."""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

from draw_things_control.core.global_config import PROJECT_ROOT

# Fixed, not configurable: every process must share one lock file, whatever configuration it loads.
STATE_DIRECTORY = PROJECT_ROOT / "state"
LOCK_FILE_NAME = "run.lock"
EX_TEMPFAIL = 75
# A read-only screen probes the lock by holding it for an instant, so a busy lock is retried this long before it is reported.
BUSY_RETRY_SECONDS = 0.25
_RETRY_INTERVAL_SECONDS = 0.025
CHILD_EXECUTABLE_NAME = "draw-things-cli"


class RunLockBusy(Exception):
    """Another run holds the lock, or its draw-things-cli child is still running."""


class RunLockError(Exception):
    """The lock file cannot be created or locked, for a reason other than being busy."""


def state_directory() -> Path:
    """The directory of the database and the lock; read on each call so tests can point it elsewhere."""
    return STATE_DIRECTORY


def ensure_state_directory() -> Path:
    """Create the state directory if needed and return it."""
    directory = state_directory()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RunLockError(f"Cannot create the state directory {directory}: {error.strerror}") from error
    return directory


def is_draw_things_process(pid: int, name: str = CHILD_EXECUTABLE_NAME) -> bool:
    """Whether ``pid`` is alive and its command is ``name`` (the executable the run started), so a reused PID is not mistaken for one."""
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True, check=False, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    command = Path(result.stdout.strip()).name
    # Some systems truncate the command name to 15 characters.
    return result.returncode == 0 and bool(command) and (command == name or (len(command) == 15 and name.startswith(command)))


def _process_group_alive(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunLock:
    """An exclusive ``flock`` on ``state/run.lock``, held for a whole job, cooldowns included.

    The operating system releases the lock when the process exits, however it exits. The file also names the
    holder (line 1: command and PID) and, while a run's child exists, the child's PID (line 2); a starter
    that finds an earlier holder's child still running refuses to start, since ``SIGKILL`` on the holder
    releases the lock but not the GPU.
    """

    def __init__(self, command: str, *, directory: Path | None = None, retry_seconds: float = BUSY_RETRY_SECONDS, child_check: Callable[[int, str], bool] = is_draw_things_process) -> None:
        self._command = command
        self._directory = directory
        self._retry_seconds = retry_seconds
        self._child_check = child_check
        self._descriptor: int | None = None

    def acquire(self) -> None:
        """Take the lock, or raise RunLockBusy (held, or an earlier run's child is alive) or RunLockError."""
        if self._descriptor is not None:
            raise RuntimeError("This run lock is already held")
        descriptor = self._open()
        try:
            if not self._flock(descriptor):
                raise RunLockBusy(self._busy_message(descriptor))
            orphan = self._orphaned_child(descriptor)
            if orphan is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                raise RunLockBusy(f"A {orphan[1]} from an earlier run is still running (PID {orphan[0]}). Wait for it to end or stop it.")
            self._write(descriptor, f"{self._command} {os.getpid()}\n")
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor

    def release(self) -> None:
        """Clear the holder and child lines and drop the lock; safe to call when not held."""
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        try:
            os.ftruncate(descriptor, 0)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def record_child(self, pid: int, name: str = CHILD_EXECUTABLE_NAME) -> None:
        """Note the PID and executable name of the run's child in the lock file."""
        if self._descriptor is not None:
            self._write(self._descriptor, f"{self._command} {os.getpid()}\n{pid} {name}\n")

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None) -> None:
        self.release()

    def _open(self) -> int:
        directory = self._directory if self._directory is not None else ensure_state_directory()
        path = directory / LOCK_FILE_NAME
        try:
            return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as error:
            raise RunLockError(f"Cannot open the run lock {path}: {error.strerror}") from error

    def _flock(self, descriptor: int) -> bool:
        deadline = time.monotonic() + self._retry_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(_RETRY_INTERVAL_SECONDS)
            except OSError as error:
                raise RunLockError(f"Cannot lock the run lock file (does this filesystem support flock?): {error.strerror}") from error

    def _orphaned_child(self, descriptor: int) -> tuple[int, str] | None:
        """The (PID, name) of an earlier holder's child that is still running, or None."""
        lines = self._read(descriptor).splitlines()
        parts = lines[1].split(maxsplit=1) if len(lines) >= 2 else []
        if not parts or not parts[0].isdigit():
            return None
        pid, name = int(parts[0]), parts[1].strip() if len(parts) == 2 else CHILD_EXECUTABLE_NAME
        return (pid, name) if pid > 1 and _process_group_alive(pid) and self._child_check(pid, name) else None

    def _busy_message(self, descriptor: int) -> str:
        lines = self._read(descriptor).splitlines()
        parts = lines[0].split() if lines else []
        holder = f" ({parts[0]}, PID {parts[1]})" if len(parts) == 2 and parts[1].isdigit() else ""
        return f"Another run is in progress{holder}. Try again when it finishes."

    @staticmethod
    def _read(descriptor: int) -> str:
        return os.pread(descriptor, 4096, 0).decode("utf-8", errors="replace")

    @staticmethod
    def _write(descriptor: int, text: str) -> None:
        # Write, then cut to length: the file is never empty in between, so a starter that is refused always reads the holder.
        data = text.encode("utf-8")
        os.pwrite(descriptor, data, 0)
        os.ftruncate(descriptor, len(data))


def run_lock_is_free(*, directory: Path | None = None) -> bool:
    """Whether no run holds the lock, for read-only screens; the file is left as it is.

    The probe holds the lock for an instant, which RunLock's retry window absorbs.
    """
    try:
        descriptor = os.open((directory or state_directory()) / LOCK_FILE_NAME, os.O_RDWR)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except OSError:
        return False
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return True
    finally:
        os.close(descriptor)
