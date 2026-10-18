"""Tests for the argument tables of the dry-run plan and the run events, for both backends."""

from __future__ import annotations

import unittest
from pathlib import Path

from framecontrol.jobs.backends.h3 import H3Arguments, H3Settings
from framecontrol.tui.text.arguments import DEFAULT_NOTE, argument_rows, arguments_text


def h3_command(**settings: object) -> tuple[str, ...]:
    values = H3Settings(768, 1344, 96, 4, 30, 42, 90, **settings)  # type: ignore[arg-type]
    return H3Arguments(Path("/h3"), Path("/model"), "a prompt", "i2av", (), Path("/input.png"), Path("/out.mp4"), values, "ffmpeg", "ffprobe").command


class H3ArgumentRowTests(unittest.TestCase):
    def test_reuse_and_core_reuse_show_their_values_not_yes(self) -> None:
        rows = {flag: value for flag, value, _note in argument_rows(h3_command(denoise_reuse=2, dit_layers=40), {})[0]}
        self.assertEqual((rows["--reuse"], rows["--layers"], rows["--core-reuse"]), ("2", "40", "1"))
        self.assertNotIn("yes", [rows[flag] for flag in ("--reuse", "--layers", "--core-reuse")])

    def test_a_knob_left_at_its_default_is_listed_as_not_passed_and_an_explicit_one_is_not(self) -> None:
        flags = argument_rows(h3_command(core_reuse=3), {})[0]
        self.assertIn(("--reuse", "1", DEFAULT_NOTE), flags)
        self.assertIn(("--layers", "50", DEFAULT_NOTE), flags)
        self.assertIn(("--core-reuse", "3", ""), flags)

    def test_the_prompt_is_left_out_and_no_config_rows_exist(self) -> None:
        flags, config = argument_rows(h3_command(), {})
        self.assertNotIn("-p", [flag for flag, _value, _note in flags])
        self.assertEqual(config, [])

    def test_a_later_run_shows_only_the_changed_knob(self) -> None:
        first, second = argument_rows(h3_command(), {}), argument_rows(h3_command(denoise_reuse=2), {})
        text = str(arguments_text(second, (1, first)))
        self.assertTrue(text.startswith("Arguments as run 1, except:"), text)
        self.assertIn("--reuse", text)
        self.assertNotIn("--steps", text)

    def test_a_draw_things_command_is_read_as_before(self) -> None:
        flags, config = argument_rows(("draw-things-cli", "generate", "--steps", "8", "--config-json", '{"shift": 5}'), {})
        self.assertEqual([(flag, value) for flag, value, _note in flags], [("--steps", "8")])
        self.assertEqual([(key, value) for key, value, _note in config], [("shift", "5")])
