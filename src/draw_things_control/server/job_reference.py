"""Resolving ``{job}`` (a job ID or a file name in ``data/jobs/``) to a path, and the listing the API shows. The
server reads regular files only: a symbolic link is listed as invalid ('is a symbolic link') and never read, since
a YAML error quotes lines of the file it read (Milestone 02)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from draw_things_control.core.errors import InputError
from draw_things_control.services.job_catalog import JobCatalog, JobListing, JobRow

SYMLINK_MESSAGE = "is a symbolic link"


def resolve_job_reference(catalog: JobCatalog, name: str) -> Path:
    """The path ``name`` (a job ID or file name) resolves to. Raises NotFoundError or InputError from
    ``JobCatalog.find`` (an unknown or ambiguous reference), or InputError naming it a symbolic link, before it is
    ever read."""
    path = catalog.find(name)
    if path.is_symlink():
        raise InputError(f"{path.name} {SYMLINK_MESSAGE}", path=path)
    return path


def listing_rows(listing: JobListing) -> list[JobRow]:
    """``listing.rows``, each symbolic link replaced with an invalid row naming it one, never its followed-through
    content (``JobCatalog`` itself follows a symbolic link to read and validate it, as ``Path.is_file()`` does)."""
    return [replace(row, job=None, error=f"{row.path.name} {SYMLINK_MESSAGE}", error_field=None) if row.path.is_symlink() else row for row in listing.rows]
