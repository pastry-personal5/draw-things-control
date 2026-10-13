"""Process groups: is one alive, and sending it a signal."""

from __future__ import annotations

import errno
import os
import signal


def process_group_alive(process_group_id: int, *, denied_means_alive: bool) -> bool:
    """Whether the process group still has a process.

    ``denied_means_alive`` says what a group we may not signal is: the runner created its group, so a group it may not
    signal is unavailable to it (False); the run lock only asks whether a group is there at all (True).
    """
    if os.name != "posix":
        return False
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return denied_means_alive
    return True


def send_to_process_group(process_group_id: int, sig: signal.Signals) -> None:
    """Send ``sig`` to the group; a group that is gone, or that we may not signal, is not an error."""
    if os.name == "posix":
        try:
            os.killpg(process_group_id, sig)
        except OSError as error:
            if error.errno not in (errno.ESRCH, errno.EPERM):
                raise
