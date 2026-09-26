"""Watching the job files and what they read, so the Job Definition widget reads the directory only when something changed.

Watchdog uses the operating system's notifications (FSEvents on macOS, inotify on Linux), so nothing is polled. No widgets:
the observer calls back on its own thread.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from loguru import logger
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver, ObservedWatch

from draw_things_control.jobs.job_report import JOB_SUFFIXES

# Events that change nothing: inotify reports reads, including the catalog's own.
IGNORED_EVENTS = frozenset({"opened", "closed_no_write"})


def resolved(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return path


class JobWatcher(FileSystemEventHandler):
    """Calls ``changed`` when a job file in ``directory`` is added, changed, renamed, or deleted, or when a file a valid job
    reads (``follow``) changes. Each directory is watched without its sub-directories, and only while it exists.

    ``complete`` is false when a directory to watch is missing or could not be watched, so the caller checks on a timer
    instead. No method raises: a watch that fails is logged and left to that timer.
    """

    def __init__(self, directory: Path, changed: Callable[[], None]) -> None:
        super().__init__()
        self.directory = resolved(directory)
        self.changed = changed
        # ``_lock`` guards only ``_files`` and is never held while calling the observer, whose own lock is held while it
        # dispatches events to ``matters``; ``_observer_lock`` serialises the calls that change the observer.
        self._lock = threading.Lock()
        self._observer_lock = threading.Lock()
        self._stopped = False
        self._files: frozenset[Path] = frozenset()
        self._watches: dict[Path, ObservedWatch] = {}
        self._observer: BaseObserver | None = None
        self.complete = False

    def follow(self, files: Iterable[Path]) -> bool:
        """Watch the job directory and the directories of ``files``; whether every one of them is watched."""
        files = frozenset(resolved(path) for path in files)
        directories = {self.directory, *(path.parent for path in files)}
        with self._lock:
            self._files = files
        with self._observer_lock:
            if self._stopped:
                return False
            try:
                if self._observer is None:
                    self._observer = Observer()
                    self._observer.start()
                for directory in set(self._watches) - directories:
                    self._observer.unschedule(self._watches.pop(directory))
                complete = True
                for directory in directories - set(self._watches):
                    # FSEvents accepts a directory that does not exist and never reports it; the timer covers it.
                    if not directory.is_dir():
                        complete = False
                        continue
                    self._watches[directory] = self._observer.schedule(self, str(directory), recursive=False)
            except Exception as error:
                logger.warning("Cannot watch the job files, checking every few seconds instead: {}", error)
                complete = False
            self.complete = complete and all(directory.is_dir() for directory in self._watches)
            return self.complete

    def stop(self) -> None:
        with self._observer_lock:
            self._stopped = True
            observer, self._observer = self._observer, None
            self._watches.clear()
        if observer is not None:
            try:
                # Not joined: this runs on the event loop, which the observer's thread may be waiting for.
                observer.stop()
            except Exception as error:
                logger.warning("Cannot stop watching the job files: {}", error)

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in IGNORED_EVENTS:
            return
        paths = [Path(str(path)) for path in (event.src_path, event.dest_path) if path]
        # The directory itself is reported as modified with each file in it; only its going matters.
        gone = event.event_type in ("deleted", "moved") and self.directory in paths
        if gone or any(self.matters(path) for path in paths):
            self.changed()

    def matters(self, path: Path) -> bool:
        with self._lock:
            files = self._files
        if path in files:
            return True
        # A job file in the directory; any other file there (an editor's swap file) is not listed.
        return path.parent == self.directory and not path.name.startswith(".") and path.suffix.lower() in JOB_SUFFIXES
