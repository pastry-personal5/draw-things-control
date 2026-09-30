"""Tests for output file names."""

import itertools
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from draw_things_control.jobs.output_naming import corrected_output_path, first_image_path, job_file_stem, last_frame_path, next_output_path, output_name, random_four_digits, raw_last_frame_path

NOW = datetime(2026, 9, 24, 15, 30, 12)


class OutputNamingTests(unittest.TestCase):
    def test_name_format(self) -> None:
        self.assertEqual(output_name("sunset-walk", "mov", NOW, 4821), "sunset-walk-20260924-153012-4821.mov")
        self.assertEqual(last_frame_path(Path("/out/sunset-walk-20260924-153012-4821.mov")), Path("/out/sunset-walk-20260924-153012-4821-last-frame.png"))

    def test_random_number_has_four_digits(self) -> None:
        self.assertTrue(all(1000 <= random_four_digits() <= 9999 for _ in range(1000)))

    def test_existing_output_or_last_frame_draws_a_new_number(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "job-20260924-153012-1111.mov").touch()
            (root / "job-20260924-153012-2222-last-frame.png").touch()
            numbers = iter([1111, 2222, 3333, 4444])
            path = next_output_path(root, "job", "mov", lambda: NOW, lambda: next(numbers), reserved={root / "job-20260924-153012-3333.mov"})
            self.assertEqual(path.name, "job-20260924-153012-4444.mov")

    def test_manifest_stem_adds_a_number_when_taken(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(job_file_stem(root, "job", lambda: NOW, lambda: 1234), "job-20260924-153012-job")
            (root / "job-20260924-153012-job.json").touch()
            numbers = itertools.count(5000)
            self.assertEqual(job_file_stem(root, "job", lambda: NOW, lambda: next(numbers)), "job-20260924-153012-5000-job")


class ColorFileNamingTests(unittest.TestCase):
    def test_the_raw_frame_copy_and_first_image_are_named_from_their_files(self) -> None:
        output = Path("/out/job-20260924-153012-1111.mov")
        self.assertEqual(raw_last_frame_path(output).name, "job-20260924-153012-1111-last-frame-raw.png")
        self.assertEqual(corrected_output_path(output).name, "job-20260924-153012-1111-cc.mov")
        self.assertEqual(corrected_output_path(output.with_suffix(".mp4")).name, "job-20260924-153012-1111-cc.mp4")
        self.assertEqual(first_image_path(Path("/out/job-20260924-153012-job.json")).name, "job-20260924-153012-job-first-image.png")

    def test_a_taken_raw_frame_copy_or_first_image_draws_a_new_number(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "job-20260924-153012-1111-cc.mov").touch()
            (root / "job-20260924-153012-2222-last-frame-raw.png").touch()
            numbers = iter([1111, 2222, 3333])
            self.assertEqual(next_output_path(root, "job", "mov", lambda: NOW, lambda: next(numbers)).name, "job-20260924-153012-3333.mov")
            (root / "job-20260924-153012-job-first-image.png").touch()
            self.assertEqual(job_file_stem(root, "job", lambda: NOW, lambda: 6000), "job-20260924-153012-6000-job")
