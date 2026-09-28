"""What every route and the gRPC service need, built once by ``dtc serve`` and shared for the process's lifetime."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version

from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.core.paths import ProjectPaths
from draw_things_control.jobs.executor import JobExecutor
from draw_things_control.server.event_backlog import EventBacklog
from draw_things_control.services.job_catalog import JobCatalog
from draw_things_control.services.queue_worker import QueueWorker
from draw_things_control.services.store_provider import StoreProvider
from draw_things_control.state.store import Store


def server_version() -> str:
    try:
        return version("draw-things-control")
    except PackageNotFoundError:
        return "0.0.0"


class SharedStoreProvider(StoreProvider):
    """A ``StoreProvider`` over the one ``Store`` the server already holds open for its whole lifetime, so
    ``JobCatalog`` (which needs one to assign job IDs) reuses that connection instead of opening a second one to the
    same database. ``close()`` is a no-op: the server, not this adapter, owns the store's lifetime."""

    def __init__(self, store: Store) -> None:  # pylint: disable=super-init-not-called
        self._shared = store

    def get(self, *, create: bool) -> Store | None:
        return self._shared

    def close(self) -> None:
        pass


@dataclass
class ServerContext:
    """Everything a route or the gRPC service reads: the project's paths, its live global configuration, the open
    store, the running worker, an executor for previews and dry runs, the token (never served), and where the
    process is bound (for the ``Host`` header check). ``allow_write`` gates the write tools and endpoints
    (Milestone 07). ``catalog`` is built from ``store`` once ``__post_init__`` runs, so every request reuses its
    file-signature cache instead of re-reading every job file. ``event_backlog`` is shared between the HTTP routes
    (which append the queue's own events: a submit, cancel, or resume changing an entry) and the gRPC ``Monitor``
    service's ``WatchEvents``; the worker appends the job events and its own claim/finish/wait transitions into the
    same backlog, given it at construction (``QueueWorker(..., on_event=context.event_backlog.append)``).
    ``submission_lock`` serializes a submission's or resume's check against ``api_limits.max_queued_jobs`` with its
    insert: two concurrent requests each reading the queued count before either inserts could otherwise both pass a
    check the second insert alone would have failed, over-filling the queue past the limit."""

    paths: ProjectPaths
    global_config: GlobalConfig
    store: Store
    worker: QueueWorker
    executor: JobExecutor
    executable: str
    token: str
    bound_host: str
    bound_port: int
    allow_write: bool = False
    event_backlog: EventBacklog = field(default_factory=EventBacklog)
    submission_lock: threading.Lock = field(default_factory=threading.Lock)
    catalog: JobCatalog = field(init=False)

    def __post_init__(self) -> None:
        self.catalog = JobCatalog(self.paths.jobs, self.global_config, self.paths, SharedStoreProvider(self.store))
