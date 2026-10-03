"""Validation and recoverable filesystem operations for API-managed job files."""

from __future__ import annotations

import hashlib
import os
import stat
from datetime import datetime
from pathlib import Path

from draw_things_control.core.errors import ConflictError, InputError, LimitExceededError, NotFoundError
from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.core.paths import ProjectPaths, linked_component
from draw_things_control.core.yaml_files import is_yaml_file, read_bounded_bytes
from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.jobs.parsing import load_job_text
from draw_things_control.jobs.prompt_pairs import NAME_PATTERN
from draw_things_control.services.api_rules import check_job_limits, check_job_rules
from draw_things_control.state.store import Store


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_job_text(text: str, *, name: str | None, global_config: GlobalConfig, paths: ProjectPaths) -> JobDefinition:
    """Perform precisely the job checks used before an API file write, without touching disk."""
    _check_size(text, global_config)
    if name is not None:
        _check_name(name)
    path = paths.jobs / f"{name if name is not None else '<draft>'}.yaml"
    job = load_job_text(text, path, global_config, paths.params, decode_input=False)
    if name is not None and job.name != name:
        raise InputError(f"'name' must match the job text's name ({job.name!r})", field="name")
    check_job_rules(job, global_config)
    check_job_limits(job, global_config.api_limits)
    return load_job_text(text, path, global_config, paths.params, decode_input=True)


def create_job(name: str, text: str, *, global_config: GlobalConfig, paths: ProjectPaths) -> tuple[Path, JobDefinition]:
    job = validate_job_text(text, name=name, global_config=global_config, paths=paths)
    target = _target(paths, name)
    _check_no_stem_collision(paths.jobs, name, target)
    _ensure_directory(paths.jobs, paths.root)
    _atomic_create(target, text.encode("utf-8"))
    return target, job


def replace_job(name: str, text: str, expected_sha256: str, *, global_config: GlobalConfig, paths: ProjectPaths, store: Store) -> tuple[Path, bool, JobDefinition]:
    job = validate_job_text(text, name=name, global_config=global_config, paths=paths)
    target = _target(paths, name)
    _check_no_stem_collision(paths.jobs, name, target)
    _check_active(target, store)
    old = _read_current(target, expected_sha256, max_bytes=global_config.api_limits.max_job_file_bytes)
    new = text.encode("utf-8")
    if old == new:
        return target, False, job
    backup_directory = paths.jobs_backups / name
    _ensure_directory(paths.jobs_backups, paths.root)
    _ensure_directory(backup_directory, paths.root)
    backup = _unique_path(backup_directory, ".yaml")
    _exclusive_write(backup, old)
    mode = stat.S_IMODE(target.stat().st_mode)
    _atomic_replace(target, new, mode)
    return target, True, job


def delete_job(name: str, expected_sha256: str, *, paths: ProjectPaths, store: Store) -> Path:
    _check_name(name)
    target = _target(paths, name)
    _check_active(target, store)
    _check_hash_current(target, expected_sha256)
    _ensure_directory(paths.jobs_trash, paths.root)
    trash = _unique_path(paths.jobs_trash, ".yaml", prefix=f"{name}-")
    try:
        os.link(target, trash)
        target.unlink()
    except OSError:
        if trash.exists():
            trash.unlink()
        raise
    # The streamed hash check happens immediately before the link/unlink pair.
    return trash


def check_expected_sha256(value: str | None) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdefABCDEF" for character in value):
        raise InputError("'expected_sha256' must be 64 hexadecimal digits", field="expected_sha256")
    return value.lower()


def _check_size(text: str, global_config: GlobalConfig) -> None:
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError as error:
        raise InputError("'yaml' must be valid UTF-8", field="yaml") from error
    limit = global_config.api_limits.max_job_file_bytes
    if size > limit:
        raise LimitExceededError(f"{size} bytes is over the max_job_file_bytes limit of {limit}", key="max_job_file_bytes", limit=limit, value=size)


def _check_name(name: str) -> None:
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise InputError("'name' must be 1-64 lowercase letters, digits, or hyphens, starting and ending with a letter or digit", field="name")


def _target(paths: ProjectPaths, name: str) -> Path:
    _check_name(name)
    if linked_component(paths.jobs, paths.root) is not None:
        raise ConflictError("Refusing to use a symbolic link in the jobs directory")
    return paths.jobs / f"{name}.yaml"


def _check_no_stem_collision(directory: Path, name: str, target: Path) -> None:
    if not directory.exists():
        return
    for candidate in directory.iterdir():
        if candidate.name.startswith(".") or not is_yaml_file(candidate):
            continue
        if candidate.stem.casefold() == name.casefold() and candidate != target:
            raise ConflictError(f"A job file with this name already exists: {candidate.name}")
    if target.is_symlink():
        raise ConflictError(f"Refusing to write through symbolic link {target.name}")


def _ensure_directory(directory: Path, root: Path) -> None:
    if linked_component(directory, root) is not None:
        raise ConflictError("Refusing to use a symbolic link in the jobs directory")
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise ConflictError(f"Refusing to use non-directory {directory}")


def _check_active(target: Path, store: Store) -> None:
    resolved = str(target.resolve())
    if any(Path(row.job_path).resolve() == Path(resolved) for row in store.queue.list_active()):
        raise ConflictError(f"Job file {target.name} is queued or running")


def _read_current(target: Path, expected: str, *, max_bytes: int | None = None) -> bytes:
    if target.is_symlink():
        raise ConflictError(f"Refusing to use symbolic link {target.name}")
    try:
        data = target.read_bytes() if max_bytes is None else read_bounded_bytes(target, max_bytes, "Job file")
    except FileNotFoundError as error:
        raise NotFoundError(f"No job file '{target.stem}'") from error
    actual = sha256_bytes(data)
    if actual != expected:
        error = ConflictError("Job file changed since it was read")
        error.current_sha256 = actual  # type: ignore[attr-defined]  # response-only detail
        raise error
    return data


def _check_hash_current(target: Path, expected: str) -> None:
    """Check a deletion's expected hash without loading an arbitrarily large local file into memory."""
    if target.is_symlink():
        raise ConflictError(f"Refusing to use symbolic link {target.name}")
    digest = hashlib.sha256()
    try:
        with target.open("rb") as file:
            for chunk in iter(lambda: file.read(65536), b""):
                digest.update(chunk)
    except FileNotFoundError as error:
        raise NotFoundError(f"No job file '{target.stem}'") from error
    actual = digest.hexdigest()
    if actual != expected:
        error = ConflictError("Job file changed since it was read")
        error.current_sha256 = actual  # type: ignore[attr-defined]  # response-only detail
        raise error


def _unique_path(directory: Path, suffix: str, *, prefix: str = "") -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for number in range(1, 10000):
        ending = "" if number == 1 else f"-{number}"
        candidate = directory / f"{prefix}{stamp}{ending}{suffix}"
        if not candidate.exists():
            return candidate
    raise ConflictError(f"Cannot find an unused recovery name in {directory}")


def _exclusive_write(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
    except BaseException:
        if path.exists():
            path.unlink()
        raise


def _temporary_path(target: Path) -> Path:
    for number in range(1, 10000):
        candidate = target.parent / f".{target.name}.{os.getpid()}.{number}.tmp"
        if not candidate.exists():
            return candidate
    raise ConflictError(f"Cannot find an unused temporary name in {target.parent}")


def _atomic_create(target: Path, data: bytes) -> None:
    temporary = _temporary_path(target)
    _exclusive_write(temporary, data)
    try:
        os.link(temporary, target)
    except FileExistsError as error:
        raise ConflictError(f"Job file {target.name} already exists") from error
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_replace(target: Path, data: bytes, mode: int) -> None:
    temporary = _temporary_path(target)
    try:
        _exclusive_write(temporary, data)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
