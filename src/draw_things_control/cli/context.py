"""Shared by every command module in ``cli/``: the services object a command reads from its Typer context, and how
an error ends one. Its own module, not ``cli/app.py``, so ``cli/queue_app.py`` (Milestone 03) can import it without
``cli/app.py`` importing ``cli/queue_app.py`` back to register it -- a cycle."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import typer
from loguru import logger

from draw_things_control.core.errors import DtcError
from draw_things_control.core.exit_codes import EXIT_INVALID_INPUT, exit_code_for_error
from draw_things_control.core.paths import DEFAULT_PATHS, ProjectPaths
from draw_things_control.services.toolkit import Toolkit

if TYPE_CHECKING:
    import httpx


@dataclass(frozen=True)
class CliServices:
    """What every command needs from the machine: where the project's files are, and the real tools. ``main``
    builds it once; a test passes its own with ``CliRunner.invoke(..., obj=...)``.

    ``http_transport`` and ``grpc_stub_factory`` are ``dtc queue``'s own test seams (Milestone 03), both None in
    production: a transport its httpx client uses instead of the network, and a factory for the gRPC stub
    ``add --wait`` streams ``WatchQueueEntry`` from instead of dialing a real ``dtc serve``.
    """

    paths: ProjectPaths
    toolkit: Toolkit
    http_transport: "httpx.BaseTransport | None" = None
    grpc_stub_factory: Callable[[str], object] | None = None


def services_of(ctx: typer.Context) -> CliServices:
    """The context's services, or the project's own when a command runs without ``main``."""
    return ctx.obj if isinstance(ctx.obj, CliServices) else CliServices(DEFAULT_PATHS, Toolkit())


@contextmanager
def errors_exit() -> Iterator[None]:
    """Log an error that stops a command (invalid input, a busy run lock, an unusable state store or server) and
    exit with its code."""
    try:
        yield
    except DtcError as error:
        logger.error("{}", error)
        raise typer.Exit(code=exit_code_for_error(error)) from error
    except ValueError as error:
        logger.error("{}", error)
        raise typer.Exit(code=EXIT_INVALID_INPUT) from error
