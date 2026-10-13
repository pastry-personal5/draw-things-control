"""Private, digest-checked snapshots of Ref2VA input assets."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import uuid
from pathlib import Path
from time import monotonic
from typing import Any, cast

from framechain.jobs.backends.h3 import H3JobData
from framechain.jobs.definition import JobDefinition


def snapshot_references(job: JobDefinition, directory: Path, max_file_bytes: int, max_total_bytes: int) -> tuple[Path | None, list[dict[str, Any]]]:
    """Copy every declared reference from its opened regular-file descriptor into private storage."""
    if job.backend != "h3" or not isinstance(job.backend_data, H3JobData) or not job.backend_data.reference_pairs:
        return None, []
    root = directory / f"h3-references-{uuid.uuid4().hex}"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(directory_fd, 0o700)
        os.mkdir(root.name, 0o700, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    descriptors: list[dict[str, Any]] = []
    total = 0
    deadline = monotonic() + 600
    try:
        for pair_index, pair in enumerate(job.backend_data.reference_pairs):
            for ordinal, reference in enumerate(pair.references):
                for field, source in (("path", reference.path), ("audio", reference.audio)):
                    if source is None:
                        continue
                    expected = reference.sha256 if field == "path" else reference.audio_sha256
                    descriptor = _copy_one(source, job.backend_data.input_directory, root, pair_index, ordinal, field, pair.name, reference.kind, reference.media_facts or {}, expected, max_file_bytes, max_total_bytes - total, deadline)
                    total += descriptor["size"]
                    descriptors.append(descriptor)
        return root, descriptors
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def verify_snapshot(descriptors: list[dict[str, Any]]) -> dict[tuple[str, int, str], Path]:
    """Verify frozen copy identity and return parser override paths keyed by pair/ordinal/field."""
    overrides: dict[tuple[str, int, str], Path] = {}
    for descriptor in descriptors:
        path = Path(cast(str, descriptor["snapshot_path"]))
        field = f"{descriptor['pair']}:{descriptor['ordinal']}:{descriptor['field']}"
        try:
            if not path.is_absolute() or not path.parent.name.startswith("h3-references-") or path.parent.parent.name != "queue-inputs":
                raise ValueError("snapshot copy is missing or not a regular file")
            digest = hashlib.sha256()
            size = 0
            root_fd = os.open(path.parent.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                pair_fd = os.open(path.parent.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                try:
                    copy_fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=pair_fd)
                finally:
                    os.close(pair_fd)
            finally:
                os.close(root_fd)
            with os.fdopen(copy_fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("snapshot copy is missing or not a regular file")
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    size += len(block)
                    digest.update(block)
            if size != descriptor["size"] or digest.hexdigest() != descriptor["sha256"]:
                raise ValueError("snapshot copy digest or size changed")
        except OSError as error:
            raise ValueError(f"H3 reference snapshot {field} cannot be read: {error}") from error
        except ValueError as error:
            raise ValueError(f"H3 reference snapshot {field}: {error}") from error
        overrides[(str(descriptor["pair"]), int(descriptor["ordinal"]), str(descriptor["field"]))] = path
    return overrides


def cleanup_snapshot(descriptors: list[dict[str, Any]]) -> None:
    roots = {Path(cast(str, item["snapshot_path"])).parent for item in descriptors}
    for root in roots:
        shutil.rmtree(root, ignore_errors=True)


def _open_confined(source: Path, input_directory: Path) -> int:
    """Open every path component relative to the validated input root without following links."""
    try:
        relative = source.relative_to(input_directory)
    except ValueError as error:
        raise ValueError(f"reference path is outside input directory: {source}") from error
    if not relative.parts or any(part in (".", "..") for part in relative.parts):
        raise ValueError(f"invalid reference path: {source}")
    directory_fd = os.open(input_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return os.open(relative.parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def _copy_one(source: Path, input_directory: Path, root: Path, pair_index: int, ordinal: int, field: str, pair: str, kind: str, media_facts: dict[str, Any], expected_sha256: str | None, file_limit: int, remaining: int, deadline: float) -> dict[str, Any]:
    if remaining < 0:
        raise ValueError("H3 reference assets exceed max_reference_bytes")
    try:
        source_fd = _open_confined(source, input_directory)
    except (OSError, ValueError) as error:
        raise ValueError(f"H3 reference {pair}[{ordinal}].{field} cannot be opened safely: {error}") from error
    name = f"{pair_index:02d}-{ordinal:02d}-{field}-{source.name}"
    destination = root / name
    temporary = root / f".{name}.{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256()
    copied = 0
    try:
        info = os.fstat(source_fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"H3 reference {pair}[{ordinal}].{field} is not a regular file")
        if info.st_size > file_limit:
            raise ValueError(f"H3 reference {pair}[{ordinal}].{field} is over max_reference_file_bytes")
        if info.st_size > remaining:
            raise ValueError(f"H3 reference {pair}[{ordinal}].{field} makes assets exceed max_reference_bytes")
        output_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(source_fd, "rb", closefd=False) as source_stream, os.fdopen(output_fd, "wb") as output_stream:
                for block in iter(lambda: source_stream.read(1024 * 1024), b""):
                    if monotonic() > deadline:
                        raise ValueError("H3 reference snapshot exceeded the 600-second submission limit")
                    copied += len(block)
                    if copied > file_limit or copied > remaining:
                        raise ValueError(f"H3 reference {pair}[{ordinal}].{field} exceeded a snapshot byte limit while copying")
                    digest.update(block)
                    output_stream.write(block)
                output_stream.flush()
                os.fsync(output_stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
            temporary.unlink(missing_ok=True)
            raise ValueError(f"H3 reference {pair}[{ordinal}].{field} changed after validation")
        os.replace(temporary, destination)
    finally:
        os.close(source_fd)
    return {"pair": pair, "ordinal": ordinal, "field": field, "kind": kind, "snapshot_role": "ref2av_reference", "media_facts": media_facts, "basename": source.name, "snapshot_path": str(destination), "sha256": digest.hexdigest(), "size": copied}
