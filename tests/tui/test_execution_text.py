"""Tests for the execution detail text: a resumed execution shows what it resumes and its first run."""

from __future__ import annotations

import unittest
from pathlib import Path

from framecontrol.jobs.backends.h3 import H3Arguments, H3Settings
from framecontrol.state.timing import StageRow
from framecontrol.tui.text.execution import execution_detail, execution_detail_text, execution_text
from tests.fixtures import execution_row, run_row


class ExecutionTextResumeTests(unittest.TestCase):
    def test_a_plain_execution_shows_neither_field(self) -> None:
        text = str(execution_text(execution_row()))
        self.assertNotIn("resumes:", text)
        self.assertNotIn("first run:", text)

    def test_a_resumed_execution_shows_what_it_resumes_and_its_first_run(self) -> None:
        text = str(execution_text(execution_row(first_run=4, resumes=3)))
        self.assertIn("resumes: E0003\n", text)
        self.assertIn("first run: 4\n", text)

    def test_the_execution_widgets_detail_shows_what_it_resumes(self) -> None:
        plain = execution_detail(execution_row())
        self.assertIsNone(plain.resumes)
        self.assertNotIn("resumes", str(execution_detail_text(plain, 40, None)[0]))
        resumed = execution_detail(execution_row(resumes=3))
        self.assertEqual(resumed.resumes, "E0003")
        self.assertIn("resumes E0003", str(execution_detail_text(resumed, 40, None)[0]))


class ExecutionTextColorTests(unittest.TestCase):
    def test_the_first_image_and_each_runs_corrected_copy_and_anchor_are_shown_when_there_are_any(self) -> None:
        plain = str(execution_text(execution_row(runs=(run_row(output="a.mov"),))))
        self.assertNotIn("first image:", plain)
        self.assertNotIn("corrected copy:", plain)
        row = execution_row(first_image="/out/walk-job-first-image.png", runs=(run_row(output="a.mov", last_frame="a-last-frame.png", corrected_output="a-cc.mov", anchor="/out/walk-job-first-image.png"),))
        text = str(execution_text(row))
        self.assertIn("first image: /out/walk-job-first-image.png\n", text)
        self.assertIn("corrected copy: ", text)
        self.assertIn("a-cc.mov", text)
        self.assertIn("anchor: /out/walk-job-first-image.png", text)


class ExecutionDetailStagesTests(unittest.TestCase):
    """The stages of the selected run, under the runs: each with its units, time, and share of the run."""

    @staticmethod
    def rows(*specs: tuple[str, str, float, float, int | None, int | None, str | None]) -> tuple[StageRow, ...]:
        return tuple(StageRow(1, stage, None, source, start, end, done, total, unit, ()) for stage, source, start, end, done, total, unit in specs)

    def detail(self, **options: object):
        stages = self.rows(("prepare", "framecontrol", 0.0, 1.0, None, None, None), ("denoise", "child", 1.0, 91.0, 30, 30, "steps"), ("ffmpeg", "child", 91.0, 99.0, 243, 243, "frames"), ("verify", "framecontrol", 99.0, 100.0, None, None, None))
        return execution_detail(execution_row(runs=(run_row(number=1, seconds=100.0, output="a.mov"),)), {1: stages}, **options)  # type: ignore[arg-type]

    def test_the_selected_run_lists_each_stage_with_its_units_time_and_share(self) -> None:
        lines = str(execution_detail_text(self.detail(), 60, 1)[0]).split("\n")
        start = lines.index("Stages of run 1")
        self.assertEqual([line.split() for line in lines[start + 1 : start + 5]], [["prepare", "1.0", "s", "1.0%"], ["denoise", "30/30", "steps", "90", "s", "90.0%"], ["ffmpeg", "243/243", "frames", "8.0", "s", "8.0%"], ["verify", "1.0", "s", "1.0%"]])
        self.assertTrue(all(len(line) <= 60 for line in lines))

    def test_no_run_selected_or_a_run_with_no_stages_shows_none(self) -> None:
        self.assertNotIn("Stages of", str(execution_detail_text(self.detail(), 60, None)[0]))
        bare = execution_detail(execution_row(runs=(run_row(number=1, seconds=100.0, output="a.mov"),)), {})
        self.assertNotIn("Stages of", str(execution_detail_text(bare, 60, 1)[0]))

    def test_a_run_in_progress_adds_the_stage_the_forecast_says_it_is_in(self) -> None:
        text = str(execution_detail_text(self.detail(), 60, 1, running="denoise 12/30")[0])
        self.assertIn("now: denoise 12/30\n\nArguments of run 1", text)
        self.assertNotIn("now:", str(execution_detail_text(self.detail(), 60, 1)[0]))


class ExecutionDetailArgumentsTests(unittest.TestCase):
    """The selected run's arguments at the bottom of the detail, for either backend."""

    @staticmethod
    def h3_command(**settings: object) -> tuple[str, ...]:
        values = H3Settings(768, 1344, 96, 4, 30, 42, 90, **settings)  # type: ignore[arg-type]
        return H3Arguments(Path("/h3"), Path("/model"), "a --reuse prompt", "i2av", (), Path("/input.png"), Path("/out.mp4"), values, "ffmpeg", "ffprobe").command

    def lines(self, command: tuple[str, ...], selected: int | None = 1) -> list[str]:
        detail = execution_detail(execution_row(runs=(run_row(number=1, output="a.mp4", command=command),)))
        text = str(execution_detail_text(detail, 60, selected)[0])
        return text[text.index("Arguments of run 1") :].split("\n")[1:]

    def test_an_h3_run_lists_its_flags_with_their_values_and_the_defaults_it_did_not_pass(self) -> None:
        lines = self.lines(self.h3_command(denoise_reuse=2))
        self.assertIn("--reuse 2", lines)
        self.assertIn("--layers 50 (default)", lines)
        self.assertIn("--core-reuse 1 (default)", lines)
        self.assertIn("--steps 30", lines)
        self.assertFalse(any("prompt" in line for line in lines))
        self.assertNotIn("--reuse yes", lines)

    def test_an_explicit_default_is_a_plain_row(self) -> None:
        lines = self.lines(self.h3_command(core_reuse=3, explicit=frozenset({"dit_layers"})))
        self.assertIn("--core-reuse 3", lines)
        self.assertIn("--layers 50", lines)

    def test_a_draw_things_run_lists_its_flags_and_config_keys_and_no_h3_defaults(self) -> None:
        lines = self.lines(("draw-things-cli", "generate", "--prompt", "walk", "--steps", "8", "--config-json", '{"shift": 5}'))
        self.assertIn("--steps 8", lines)
        self.assertIn("shift 5", lines)
        self.assertFalse(any("(default)" in line for line in lines))

    def test_a_run_without_a_saved_command_shows_a_dash_and_every_line_fits(self) -> None:
        self.assertEqual(self.lines(())[0], "-")
        self.assertTrue(all(len(line) <= 60 for line in self.lines(self.h3_command())))

    def test_the_section_follows_the_selected_run(self) -> None:
        detail = execution_detail(execution_row(runs=(run_row(number=1, output="a.mp4"), run_row(number=2, output="b.mp4"))))
        self.assertNotIn("Arguments of run", str(execution_detail_text(detail, 60, None)[0]))
        self.assertIn("Arguments of run 2", str(execution_detail_text(detail, 60, 2)[0]))
