"""The exit codes of the commands, and how a signal becomes one."""

from __future__ import annotations

import signal

EXIT_STATE_UNAVAILABLE = 1
EXIT_INVALID_INPUT = 2
# sysexits.h EX_TEMPFAIL: another run holds the lock, so try again later.
EXIT_BUSY = 75
EXIT_TIMEOUT = 124
# A shell reports a process that a signal ended as 128 plus the signal's number.
SIGNAL_EXIT_BASE = 128


def exit_code_for_signal(received: signal.Signals) -> int:
    """The exit code of a job or command that ``received`` stopped: 130 for SIGINT, 143 for SIGTERM."""
    return SIGNAL_EXIT_BASE + received.value


def signal_for_exit_code(exit_code: int) -> signal.Signals | None:
    """The signal a 128+N exit code stands for, or None."""
    try:
        return signal.Signals(exit_code - SIGNAL_EXIT_BASE) if exit_code > SIGNAL_EXIT_BASE else None
    except ValueError:
        return None


def exit_code_for_child_signal(return_code: int) -> int:
    """The exit code for a child that a signal from outside ended (a negative return code): 137 for -9."""
    return SIGNAL_EXIT_BASE - return_code
