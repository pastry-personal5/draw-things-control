"""The job files in a data directory: listed with their validity and job IDs, and found by name or ID."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from draw_things_control.core.clock import local_timestamp
from draw_things_control.core.errors import InputError, NotFoundError
from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.core.paths import ProjectPaths
from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.jobs.files import job_files, read_job
from draw_things_control.services.job_details import error_text
from draw_things_control.services.store_provider import StoreProvider
from draw_things_control.state.ids import JOB_LETTER, job_id_text, parse_typed_id
from draw_things_control.state.store import Store


@dataclass(frozen=True)
class JobRow:
    """One job file: the job, or why it is invalid; its job ID's number, when it has one; and its modification time."""

    path: Path
    job: JobDefinition | None
    error: str | None = None
    # The offending key, when ``error`` came from an InputError (Milestone 02's GET /jobs reports it); None for a
    # plain read failure (an unreadable directory) or when there is no error.
    error_field: str | None = None
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


# A file's modification time and size, or None when it does not exist; a change in either means reading it again.
Signature = tuple[int, int] | None


def signature(path: Path) -> Signature:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


class JobCatalog:
    """The data directory's job files, read on worker threads (``find`` also on the main thread).

    Only the project's data/jobs/ gives job IDs (owner decision); another data directory lists its files without them. A
    valid job is read and validated again only when its file, its input image, or its base configuration changed (time,
    size, or whether it exists), so each check stays cheap and still notices a job turning valid or invalid; an invalid
    one, whose inputs are not known, is read every time. ``fresh`` reads every file. The IDs are the only state-store
    write browsing makes. ``read`` never raises, since a failed front-end worker closes the app, and with it a running job.
    """

    def __init__(self, directory: Path, settings: GlobalConfig, paths: ProjectPaths, store: StoreProvider) -> None:
        self.directory = directory
        self.settings = settings
        self._paths = paths
        self._store = store
        self._lock = threading.Lock()
        # Each listed file: its signature, its inputs' signatures (None for an invalid job), and its row.
        self._cache: dict[Path, tuple[Signature, tuple[tuple[Path, Signature], ...] | None, JobRow]] = {}

    @property
    def gives_ids(self) -> bool:
        try:
            return self.directory.expanduser().resolve() == self._paths.jobs.resolve()
        except OSError:
            return False

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
                numbers = self._store_for_ids().job_ids.assign([path.name for path in paths], local_timestamp(datetime.now()))
            except Exception as error:
                # Not only the store's errors: an uncreatable state directory raises RunLockError, and a worker must not raise.
                id_error = f"Cannot give job IDs: {error}"
        rows = [replace(row, number=numbers.get(row.path.name)) for row in rows]
        return JobListing(rows, None if rows else f"No job files (*.yaml, *.yml) in {directory}", id_error, reads)

    def find(self, name: str, rows: Sequence[JobRow] = ()) -> Path:
        """The job file ``name`` names: a file name, a unique name without the suffix, or a job ID (J0001, in any case)
        among ``rows`` or, before they are read, in the store. Raises NotFoundError, or InputError when the name is both a
        file's and another file's job ID, which names both, so neither is run by mistake."""
        by_file = self._find_file(name)
        number = parse_typed_id(name, JOB_LETTER)
        if number is None:
            if by_file is None:
                raise NotFoundError(f"No job file '{name}' in {self.directory}")
            return by_file
        by_id = self._find_id(number, rows)
        if by_file is None:
            if isinstance(by_id, str):
                raise NotFoundError(by_id)
            return by_id
        if isinstance(by_id, Path) and by_id != by_file:
            other = next((form for form in (job_id_text(number), f"{JOB_LETTER}{number}", f"{JOB_LETTER}{number:05d}") if form.lower() != name.lower()), job_id_text(number))
            raise InputError(f"'{name}' is both the job file {by_file.name} and the job ID {job_id_text(number)} ({by_id.name}); type {by_file.name} or {other}")
        return by_file

    def _find_file(self, name: str) -> Path | None:
        """The file with this name, or the only one with it before its suffix; None when there is none."""
        try:
            paths = job_files(self.directory) if self.directory.is_dir() else []
        except OSError as error:
            raise InputError(f"Cannot read the data directory {self.directory}: {error.strerror}") from error
        exact = next((path for path in paths if path.name == name), None)
        if exact is not None:
            return exact
        matches = [path for path in paths if path.stem == name]
        if len(matches) > 1:
            raise InputError(f"'{name}' matches {', '.join(path.name for path in matches)}; give the file name")
        return matches[0] if matches else None

    def _find_id(self, number: int, rows: Sequence[JobRow]) -> Path | str:
        """The job file with this ID, or why none (a message)."""
        match = next((row.path for row in rows if row.number == number), None)
        if match is not None:
            return match
        missing = f"No job file has the ID {job_id_text(number)} in {self.directory}"
        if not self.gives_ids:
            return missing
        try:
            store = self._store.get(create=False)
            known = store.job_ids.lookup(number) if store is not None else None
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
            job = read_job(path, self.settings, self._paths, decode_input=False)[0]
        except Exception as error:
            # Not only ValueError and OSError: a job file that breaks the loader must not end the worker.
            row, dependencies = JobRow(path, None, error_text(path, error), error_field=getattr(error, "field", None), changed=changed), None
        else:
            row = JobRow(path, job, changed=changed)
            inputs = ((job.input,) if job.input is not None else ()) + (self._paths.params / job.config_file,)
            dependencies = tuple((dependency, signature(dependency)) for dependency in inputs)
        with self._lock:
            self._cache[path] = (own, dependencies, row)
        return row

    def _store_for_ids(self) -> Store:
        store = self._store.get(create=True)
        assert store is not None
        return store
