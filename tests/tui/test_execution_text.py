"""Tests for the execution detail text: a resumed execution shows what it resumes and its first run."""

from __future__ import annotations

import unittest

from draw_things_control.tui.text.execution import execution_detail, execution_detail_text, execution_text
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
