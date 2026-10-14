"""Fork-free subprocess entry points for a process that may have live threads.

Python 3.12's ``subprocess`` uses ``fork`` whenever ``cwd`` or
``start_new_session`` is requested, and whenever the default ``close_fds`` is
kept.  A running gRPC server has C-core threads and poll descriptors that must
not cross that fork.  These wrappers instead make ``Popen`` use its
``posix_spawn`` path for a clean helper interpreter; the helper closes file
descriptors, applies the requested setup, and execs the real command while
retaining the same PID and process group.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Sequence
from typing import Any

_SPAWN_LOCK = threading.Lock()
_HELPER_MODULE = "framecontrol.core.process.exec_child"

Command = str | bytes | os.PathLike[str] | os.PathLike[bytes] | Sequence[str | bytes | os.PathLike[str] | os.PathLike[bytes]]


def _command_arguments(args: Command) -> tuple[str, ...]:
    values = (args,) if isinstance(args, (str, bytes, os.PathLike)) else tuple(args)
    if not values:
        raise ValueError("args must not be empty")
    return tuple(os.fsdecode(os.fspath(value)) for value in values)


def _read_status(fd: int) -> bytes:
    chunks: list[bytes] = []
    try:
        while chunk := os.read(fd, 4096):
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


def _close_streams(process: subprocess.Popen[Any]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def _raise_start_error(process: subprocess.Popen[Any], payload: bytes) -> None:
    process.wait()
    _close_streams(process)
    try:
        detail = json.loads(payload.decode("utf-8"))
        number = int(detail["errno"])
        message = str(detail["message"])
        filename = detail.get("filename")
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OSError(f"Child launcher failed before exec: {payload.decode(errors='replace')}") from error
    raise OSError(number, message, filename)


def popen(args: Command, bufsize: int = -1, **options: Any) -> subprocess.Popen[Any]:
    """Start ``args`` without forking the calling process on supported POSIX Python.

    The supported options are the ones Framecontrol uses.  Options that would
    run code or preserve arbitrary descriptors in the intermediate child are
    rejected rather than silently falling back to ``fork``.
    """
    if os.name != "posix" or not getattr(subprocess, "_USE_POSIX_SPAWN", False):
        return subprocess.Popen(args, bufsize=bufsize, **options)
    command = _command_arguments(args)
    cwd = options.pop("cwd", None)
    start_new_session = bool(options.pop("start_new_session", False))
    close_fds = options.pop("close_fds", True)
    pass_fds = options.pop("pass_fds", ())
    shell = options.pop("shell", False)
    executable = options.pop("executable", None)
    preexec_fn = options.pop("preexec_fn", None)
    if close_fds is False:
        raise ValueError("fork-free child launch always closes non-standard file descriptors")
    if pass_fds:
        raise ValueError("fork-free child launch does not support pass_fds")
    if shell:
        raise ValueError("fork-free child launch does not support shell=True")
    if executable is not None:
        raise ValueError("fork-free child launch does not support executable=")
    if preexec_fn is not None:
        raise ValueError("fork-free child launch does not support preexec_fn")
    for name, default in (("group", None), ("extra_groups", None), ("user", None), ("umask", -1), ("process_group", None)):
        if options.get(name, default) != default:
            raise ValueError(f"fork-free child launch does not support {name}=")
        options.pop(name, None)
    environment = options.pop("env", None)
    status_read, status_write = os.pipe()
    status_write_open = True
    process: subprocess.Popen[Any] | None = None
    try:
        with _SPAWN_LOCK:
            os.set_inheritable(status_write, True)
            helper = (
                sys.executable,
                "-I",
                "-m",
                _HELPER_MODULE,
                str(status_write),
                "1" if start_new_session else "0",
                "1" if cwd is not None else "0",
                os.fsdecode(os.fspath(cwd)) if cwd is not None else "",
                *command,
            )
            try:
                process = subprocess.Popen(helper, bufsize=bufsize, close_fds=False, env=environment, **options)
            finally:
                os.set_inheritable(status_write, False)
                os.close(status_write)
                status_write_open = False
        payload = _read_status(status_read)
    except BaseException:
        if process is None:
            os.close(status_read)
            if status_write_open:
                os.set_inheritable(status_write, False)
                os.close(status_write)
        raise
    assert process is not None
    if payload:
        _raise_start_error(process, payload)
    process.args = args
    return process


def run(args: Command, *, input: Any = None, capture_output: bool = False, timeout: float | None = None, check: bool = False, **options: Any) -> subprocess.CompletedProcess[Any]:
    """The ``subprocess.run`` contract, using :func:`popen`."""
    if input is not None:
        if options.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used.")
        options["stdin"] = subprocess.PIPE
    if capture_output:
        if options.get("stdout") is not None or options.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output.")
        options["stdout"] = subprocess.PIPE
        options["stderr"] = subprocess.PIPE
    with popen(args, **options) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        except BaseException:
            process.kill()
            raise
        return_code = process.poll()
        assert return_code is not None
        if check and return_code:
            raise subprocess.CalledProcessError(return_code, args, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(args, return_code, stdout, stderr)
