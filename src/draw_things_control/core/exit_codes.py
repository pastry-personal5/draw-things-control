"""The exit codes of the commands, and how a signal becomes one."""

from __future__ import annotations

import signal

from draw_things_control.core.errors import DtcError

EXIT_STATE_UNAVAILABLE = 1
EXIT_INVALID_INPUT = 2
# A job that parked (Milestone 05): it ended at a run boundary, keeping every run it finished, and can be resumed. Not a
# failure, and not the whole job, so ``dtc queue add --wait && next-step`` does not go on. The execution's own exit
# code can still be 3 from a failed run whose draw-things-cli exited 3; its status tells the two apart.
EXIT_PARKED = 3
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


# The exit code of each error code (see ``DtcError.code``); a front end that is not a command line maps the same codes to its own statuses.
# The four Milestone 02 codes (timeout_required, outside_directory, limit_exceeded, invalid_state) arise only from the
# API today (no CLI command raises them directly), but a front end that is one, such as dtc queue, still maps an
# API error's code to an exit code through this same table, so they are listed here too, all exiting 2.
EXIT_CODES_BY_ERROR_CODE = {
    "invalid_input": EXIT_INVALID_INPUT,
    "tool_missing": EXIT_INVALID_INPUT,
    "not_found": EXIT_INVALID_INPUT,
    "timeout_required": EXIT_INVALID_INPUT,
    "outside_directory": EXIT_INVALID_INPUT,
    "limit_exceeded": EXIT_INVALID_INPUT,
    "invalid_state": EXIT_INVALID_INPUT,
    "conflict": EXIT_INVALID_INPUT,
    "busy": EXIT_BUSY,
    "state_unavailable": EXIT_STATE_UNAVAILABLE,
}


def exit_code_for_error(error: DtcError) -> int:
    """The exit code a command ends with when ``error`` stops it; 1 for an error with no code of its own."""
    return EXIT_CODES_BY_ERROR_CODE.get(error.code, EXIT_STATE_UNAVAILABLE)


# The exit code of ``dtc queue add --wait``, drawn from the entry's own final queue state (Milestone 03), not
# EXIT_CODES_BY_ERROR_CODE above, which maps only a submission refusal. ``cancelled`` and ``interrupted`` match the
# signal that would have stopped ``run-job`` itself for the same reason: Ctrl-C (SIGINT) for a cancel, and the
# SIGTERM the server itself sends a run it stops for an interrupted one.
EXIT_CODES_BY_QUEUE_STATE = {
    "succeeded": 0,
    "cancelled": exit_code_for_signal(signal.SIGINT),
    "interrupted": exit_code_for_signal(signal.SIGTERM),
    "parked": EXIT_PARKED,
    # Shares exit 1 with EXIT_STATE_UNAVAILABLE above, but for a different reason: the job itself failed, not that
    # the state store could not be reached.
    "failed": 1,
}
