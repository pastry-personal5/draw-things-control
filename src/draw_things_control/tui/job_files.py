"""Reading the job files in the data directory for the TUI: no widgets, so it all runs on worker threads."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import overload

from draw_things_control.core import generation_config
from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.jobs.job_definition import JobDefinition
from draw_things_control.jobs.job_report import PLACEHOLDER_SEED, PlanStep, job_files, plan_header, plan_steps, read_job
from draw_things_control.jobs.job_service import JobService
from draw_things_control.state.ids import JOB_LETTER, job_id_text, parse_typed_id
from draw_things_control.state.store import Store
from draw_things_control.tui.commands import SORT_KEYS
from draw_things_control.tui.history import database_path


@dataclass(frozen=True)
class JobRow:
    """One job file: the job, or why it is invalid; its job ID's number, when it has one; and its modification time."""

    path: Path
    job: JobDefinition | None
    error: str | None = None
    number: int | None = None
    changed: float | None = None

    @property
    def job_id(self) -> str | None:
        return job_id_text(self.number) if self.number is not None else None


@dataclass(frozen=True)
class JobListing:
    """The data directory's job files, a message when there are none, why job IDs could not be given, if so, and the
    files the valid jobs read (their input images and base configurations)."""

    rows: list[JobRow] = field(default_factory=list)
    message: str | None = None
    id_error: str | None = None
    reads: frozenset[Path] = frozenset()

    @property
    def any_invalid(self) -> bool:
        return any(row.job is None for row in self.rows)


# The Job Definition widget's sort, as the state store keeps it: a key and a direction.
SORT_SETTING = "job_definition.sort"
DEFAULT_SORT = ("changed", True)


# A file's modification time and size, or None when it does not exist; a change in either means reading it again.
Signature = tuple[int, int] | None


def signature(path: Path) -> Signature:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


class JobCatalog:
    """The data directory's job files for the TUI, read on worker threads (``find`` also on the main thread).

    Only the project's data/jobs/ gives job IDs (owner decision); another data directory lists its files without them. A
    valid job is read and validated again only when its file, its input image, or its base configuration changed (time,
    size, or whether it exists), so each check stays cheap and still notices a job turning valid or invalid; an invalid
    one, whose inputs are not known, is read every time. ``fresh`` reads every file. The IDs and the sort are the
    only state-store writes browsing makes. No method raises: a failed worker closes the app, and with it a running job.
    """

    def __init__(self, directory: Path, settings: GlobalConfig) -> None:
        self.directory = directory
        self.settings = settings
        self._store: Store | None = None
        self._lock = threading.Lock()
        # Each listed file: its signature, its inputs' signatures (None for an invalid job), and its row.
        self._cache: dict[Path, tuple[Signature, tuple[tuple[Path, Signature], ...] | None, JobRow]] = {}
        # Sort saves are written one at a time, and only if no later choice was saved first.
        self._sort_lock = threading.Lock()
        self._saved_choice = 0

    @property
    def gives_ids(self) -> bool:
        try:
            return self.directory.expanduser().resolve() == generation_config.JOBS_DIRECTORY.resolve()
        except OSError:
            return False

    def close(self) -> None:
        with self._lock:
            store, self._store = self._store, None
        if store is not None:
            store.close()

    def read(self, *, fresh: bool = False) -> JobListing:
        """Every job file, validated without decoding inputs, with its job ID when this directory gives them."""
        directory = self.directory
        if not directory.is_dir():
            return JobListing(message=f"Data directory not found: {directory}")
        try:
            paths = job_files(directory)
        except OSError as error:
            return JobListing(message=f"Cannot read the data directory {directory}: {error.strerror}")
        rows = [self._row(path, fresh) for path in paths]
        listed = set(paths)
        with self._lock:
            # A deleted or renamed file is forgotten.
            self._cache = {path: entry for path, entry in self._cache.items() if path in listed}
            reads = frozenset(dependency for _, dependencies, _ in self._cache.values() for dependency, _ in dependencies or ())
        numbers: dict[str, int] = {}
        id_error = None
        if self.gives_ids:
            try:
                numbers = self._open().assign_job_ids([path.name for path in paths], datetime.now().astimezone().isoformat(timespec="seconds"))
            except Exception as error:
                # Not only the store's errors: an uncreatable state directory raises RunLockError, and a worker must not raise.
                id_error = f"Cannot give job IDs: {error}"
        rows = [replace(row, number=numbers.get(row.path.name)) for row in rows]
        return JobListing(rows, None if rows else f"No job files (*.yaml, *.yml) in {directory}", id_error, reads)

    def find(self, name: str, rows: Sequence[JobRow] = ()) -> Path | str:
        """The job file ``name`` names, or why none: a file name, a unique name without the suffix, or a job ID (J0001, in
        any case) among ``rows`` or, before they are read, in the store. A name that is both a file's and another file's
        job ID is an error naming both, so neither is run by mistake."""
        by_file = self._find_file(name)
        if isinstance(by_file, str):
            return by_file
        number = parse_typed_id(name, JOB_LETTER)
        if number is None:
            return by_file if by_file is not None else f"No job file '{name}' in {self.directory}"
        by_id = self._find_id(number, rows)
        if by_file is None:
            return by_id
        if isinstance(by_id, Path) and by_id != by_file:
            other = next((form for form in (job_id_text(number), f"{JOB_LETTER}{number}", f"{JOB_LETTER}{number:05d}") if form.lower() != name.lower()), job_id_text(number))
            return f"'{name}' is both the job file {by_file.name} and the job ID {job_id_text(number)} ({by_id.name}); type {by_file.name} or {other}"
        return by_file

    def sort(self) -> tuple[str, bool]:
        """The remembered sort (key, descending); the default when none is kept or the store cannot say."""
        try:
            store = self._open(create=False)
            value = store.setting(SORT_SETTING) if store is not None else None
        except Exception:
            return DEFAULT_SORT
        key, _, direction = (value or "").partition(" ")
        return (key, direction == "desc") if key in SORT_KEYS and direction in ("asc", "desc") else DEFAULT_SORT

    def keep_sort(self, key: str, descending: bool, choice: int) -> str | None:
        """Remember the sort across sessions, unless a later ``choice`` (they count up) was saved first; why not, when the
        store cannot."""
        with self._sort_lock:
            if choice <= self._saved_choice:
                return None
            try:
                self._open().set_setting(SORT_SETTING, f"{key} {'desc' if descending else 'asc'}")
            except Exception as error:
                return f"Cannot keep the sort: {error}"
            self._saved_choice = choice
        return None

    def _find_file(self, name: str) -> Path | str | None:
        """The file with this name, or the only one with it before its suffix; an error, or None when there is none."""
        try:
            paths = job_files(self.directory) if self.directory.is_dir() else []
        except OSError as error:
            return f"Cannot read the data directory {self.directory}: {error.strerror}"
        exact = next((path for path in paths if path.name == name), None)
        if exact is not None:
            return exact
        matches = [path for path in paths if path.stem == name]
        if len(matches) > 1:
            return f"'{name}' matches {', '.join(path.name for path in matches)}; give the file name"
        return matches[0] if matches else None

    def _find_id(self, number: int, rows: Sequence[JobRow]) -> Path | str:
        match = next((row.path for row in rows if row.number == number), None)
        if match is not None:
            return match
        missing = f"No job file has the ID {job_id_text(number)} in {self.directory}"
        if not self.gives_ids:
            return missing
        try:
            store = self._open(create=False)
            known = store.job_definition(number) if store is not None else None
        except Exception as error:
            return f"Cannot look up {job_id_text(number)}: {error}"
        if known is None:
            return missing
        path = self.directory / known[0]
        return path if path.is_file() else f"{job_id_text(number)} is {known[0]}, which is no longer in {self.directory}"

    def _row(self, path: Path, fresh: bool) -> JobRow:
        own = signature(path)
        if own is None:
            return JobRow(path, None, f"Cannot read {path.name}")
        with self._lock:
            cached = self._cache.get(path)
        if not fresh and cached is not None and cached[0] == own and cached[1] is not None and all(signature(dependency) == seen for dependency, seen in cached[1]):
            return cached[2]
        changed = own[0] / 1e9
        try:
            job = read_job(path, self.settings, decode_input=False)[0]
        except Exception as error:
            # Not only ValueError and OSError: a job file that breaks the loader must not end the worker.
            row, dependencies = JobRow(path, None, error_text(path, error), changed=changed), None
        else:
            row = JobRow(path, job, changed=changed)
            inputs = ((job.input,) if job.input is not None else ()) + (generation_config.PARAMS_DIRECTORY / job.config_file,)
            dependencies = tuple((dependency, signature(dependency)) for dependency in inputs)
        with self._lock:
            self._cache[path] = (own, dependencies, row)
        return row

    @overload
    def _open(self) -> Store: ...
    @overload
    def _open(self, *, create: bool) -> Store | None: ...
    def _open(self, *, create: bool = True) -> Store | None:
        """The state store, opened on first use without pruning, which is the job worker's to do. Only a write creates it:
        a read (``create`` false) of a database that does not exist yet is None, so browsing creates no file for it."""
        with self._lock:
            if self._store is None:
                if not create and not database_path().exists():
                    return None
                self._store = Store(retention_days=self.settings.history_retention_days, prune_on_open=False)
            return self._store


@dataclass(frozen=True)
class JobDetails:
    """What /describe job prints: the job or its error, and the plan (its header lines, then each run) or why there is none."""

    job: JobDefinition | None
    error: str | None = None
    plan: tuple[str, ...] | None = None
    plan_error: str | None = None
    runs: tuple[PlanStep, ...] = ()


def error_text(path: Path, error: Exception) -> str:
    """The error without the job file's path in front, since the message already names the file."""
    message = str(error)
    prefix = f"{path.expanduser().resolve()}: "
    return message.removeprefix(prefix)


def read_details(path: Path, settings: GlobalConfig) -> JobDetails:
    """Load the job, decoding its input, without its plan."""
    try:
        return JobDetails(read_job(path, settings)[0])
    except (ValueError, OSError) as error:
        return JobDetails(None, error_text(path, error))


def add_plan(details: JobDetails, service: JobService, executable: str) -> JobDetails:
    """The details with the job's dry-run plan, using the placeholder seed when the job sets none."""
    assert details.job is not None
    try:
        preview = service.preview(details.job, executable=executable, seed=PLACEHOLDER_SEED)
    except (ValueError, OSError) as error:
        return JobDetails(details.job, plan_error=str(error))
    job = details.job
    # The plan run-job --dry-run prints, from the same steps. The table leaves out the executable, so the header names it.
    return JobDetails(job, plan=(*plan_header(job, preview), f"Executable: {executable}"), runs=tuple(plan_steps(job, preview)))
