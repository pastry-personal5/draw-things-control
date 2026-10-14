"""Clean one-shot child used by :mod:`framecontrol.core.process.spawn`.

The parent starts this module through ``posix_spawn``.  It therefore begins in
a fresh interpreter, with none of the parent's live gRPC or Python threads,
and can safely perform the small amount of setup that ``subprocess.Popen``
would otherwise do between ``fork`` and ``exec``.
"""

from __future__ import annotations

import errno
import json
import os
import resource
import sys
from typing import NoReturn


def _descriptor_limit() -> int:
    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY:
        try:
            return int(os.sysconf("SC_OPEN_MAX"))
        except (OSError, ValueError):
            return 1_048_576
    return int(soft)


def _close_descriptors(status_fd: int) -> None:
    """Close everything but stdio and the exec-status pipe."""
    for directory in ("/proc/self/fd", "/dev/fd"):
        try:
            descriptors = [int(name) for name in os.listdir(directory) if name.isdigit()]
        except OSError:
            continue
        for descriptor in descriptors:
            if descriptor > 2 and descriptor != status_fd:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        return
    os.closerange(3, status_fd)
    os.closerange(status_fd + 1, _descriptor_limit())


def _report_error(status_fd: int, error: BaseException, filename: str | None) -> NoReturn:
    number = error.errno if isinstance(error, OSError) and error.errno is not None else errno.EINVAL
    message = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
    payload = json.dumps({"errno": number, "filename": filename, "message": message}, ensure_ascii=False).encode("utf-8", errors="replace")
    try:
        os.write(status_fd, payload)
    finally:
        os.close(status_fd)
    raise SystemExit(127)


def main() -> None:
    if len(sys.argv) < 6:
        raise SystemExit(127)
    status_fd = int(sys.argv[1])
    start_new_session = sys.argv[2] == "1"
    cwd = sys.argv[4] if sys.argv[3] == "1" else None
    command = sys.argv[5:]
    os.set_inheritable(status_fd, False)
    _close_descriptors(status_fd)
    try:
        if start_new_session:
            os.setsid()
        if cwd is not None:
            os.chdir(cwd)
        os.execvpe(command[0], command, os.environ)
    except BaseException as error:
        _report_error(status_fd, error, cwd if isinstance(error, OSError) and error.filename == cwd else command[0])


if __name__ == "__main__":
    main()
