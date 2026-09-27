"""Tests for the small modules every layer builds on: paths, errors, exit codes, the clock, numbers, and process groups."""

from __future__ import annotations

import signal
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from draw_things_control.core import exit_codes
from draw_things_control.core.clock import local_timestamp
from draw_things_control.core.errors import BusyError, DtcError, InputError, NotFoundError, StateUnavailableError, ToolMissingError
from draw_things_control.core.numbers import is_int, is_number, number_text
from draw_things_control.core.paths import DEFAULT_PATHS, ProjectPaths
from draw_things_control.core.process.groups import process_group_alive
from draw_things_control.core.run_lock import RunLockBusy, RunLockError
from draw_things_control.state.store import StateError


class PathsTests(unittest.TestCase):
    def test_every_path_is_below_the_root(self) -> None:
        paths = ProjectPaths(Path("/project"))
        self.assertEqual(
            (paths.global_config, paths.example_global_config, paths.jobs, paths.params, paths.state, paths.lock_file, paths.database),
            (Path("/project/config/global-config.yaml"), Path("/project/config/global-config.example.yaml"), Path("/project/data/jobs"), Path("/project/data/params"), Path("/project/state"), Path("/project/state/run.lock"), Path("/project/state/dtc.db")),
        )

    def test_the_default_paths_are_this_project(self) -> None:
        self.assertTrue((DEFAULT_PATHS.root / "pyproject.toml").is_file())


class ErrorTests(unittest.TestCase):
    def test_each_error_has_its_code(self) -> None:
        errors = {InputError: "invalid_input", ToolMissingError: "tool_missing", NotFoundError: "not_found", BusyError: "busy", StateUnavailableError: "state_unavailable", DtcError: "error"}
        self.assertEqual({error: error.code for error in errors}, errors)

    def test_input_errors_are_still_value_errors(self) -> None:
        for error in (InputError, ToolMissingError, NotFoundError):
            self.assertTrue(issubclass(error, ValueError))
        self.assertFalse(issubclass(BusyError, ValueError))

    def test_the_lock_and_state_errors_carry_codes(self) -> None:
        self.assertEqual((RunLockBusy.code, RunLockError.code, StateError.code), ("busy", "state_unavailable", "state_unavailable"))


class ExitCodeTests(unittest.TestCase):
    def test_a_signal_becomes_128_plus_its_number_and_back(self) -> None:
        self.assertEqual((exit_codes.exit_code_for_signal(signal.SIGINT), exit_codes.exit_code_for_signal(signal.SIGTERM)), (130, 143))
        self.assertEqual((exit_codes.signal_for_exit_code(130), exit_codes.signal_for_exit_code(143)), (signal.SIGINT, signal.SIGTERM))
        for code in (0, 2, 75, 124, 128, 300):
            self.assertIsNone(exit_codes.signal_for_exit_code(code), code)

    def test_a_child_ended_by_a_signal_from_outside(self) -> None:
        self.assertEqual(exit_codes.exit_code_for_child_signal(-9), 137)

    def test_the_documented_codes(self) -> None:
        self.assertEqual((exit_codes.EXIT_STATE_UNAVAILABLE, exit_codes.EXIT_INVALID_INPUT, exit_codes.EXIT_BUSY, exit_codes.EXIT_TIMEOUT), (1, 2, 75, 124))


class ClockAndNumberTests(unittest.TestCase):
    def test_a_local_timestamp_has_an_offset_and_no_fraction(self) -> None:
        text = local_timestamp(datetime(2026, 9, 27, 10, 5, 15, 999999))
        self.assertRegex(text, r"^2026-09-27T10:05:15[+-]\d{2}:\d{2}$")

    def test_booleans_are_not_numbers(self) -> None:
        self.assertEqual([is_int(value) for value in (1, True, 1.0, "1")], [True, False, False, False])
        self.assertEqual([is_number(value) for value in (1, 1.5, True, "1")], [True, True, False, False])

    def test_a_number_is_written_as_a_person_writes_it(self) -> None:
        self.assertEqual([number_text(value) for value in (1200, 1200.0, 90.5, 0.00001)], ["1200", "1200", "90.5", "1e-05"])


class ProcessGroupTests(unittest.TestCase):
    def test_a_group_we_may_not_signal_is_alive_only_when_asked(self) -> None:
        with mock.patch("draw_things_control.core.process.groups.os.killpg", side_effect=PermissionError):
            self.assertTrue(process_group_alive(1234, denied_means_alive=True))
            self.assertFalse(process_group_alive(1234, denied_means_alive=False))

    def test_a_missing_group_is_not_alive_either_way(self) -> None:
        with mock.patch("draw_things_control.core.process.groups.os.killpg", side_effect=ProcessLookupError):
            self.assertFalse(process_group_alive(1234, denied_means_alive=True))

    def test_a_signalable_group_is_alive(self) -> None:
        with mock.patch("draw_things_control.core.process.groups.os.killpg") as killpg:
            self.assertTrue(process_group_alive(1234, denied_means_alive=False))
        killpg.assert_called_once_with(1234, 0)
