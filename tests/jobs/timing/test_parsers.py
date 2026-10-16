"""Tests for the lines each backend prints, read as stage readings."""

import unittest

from framecontrol.jobs.timing.model import Reading
from framecontrol.jobs.timing.parsers import DrawThingsStageParser, H3StageParser


class DrawThingsParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = DrawThingsStageParser()

    def test_a_sampling_line_says_the_step_has_started_so_one_fewer_is_done(self) -> None:
        self.assertEqual(self.parser.readings("Sampling... 3 / 40 [ ] 6  %", 20.0), (Reading("sampling", "Sampling", 20.0, 2, 40, "steps", begins=True),))
        self.assertEqual(self.parser.readings("Sampling... 40 / 40 [█] 95 %", 99.0), (Reading("sampling", "Sampling", 99.0, 39, 40, "steps", begins=True),))

    def test_the_bar_lines_without_a_counter_name_their_stage_only(self) -> None:
        self.assertEqual(self.parser.readings("Starting... [ ] 0  %", 7.4), (Reading("starting", "Starting", 7.4, begins=True),))
        self.assertEqual(self.parser.readings("Processing... [ ] 2  %", 13.1), (Reading("processing", "Processing", 13.1, begins=True),))
        self.assertEqual(self.parser.readings("Finishing... [█] 100%", 2134.6), (Reading("finishing", "Finishing", 2134.6, begins=True),))

    def test_the_lines_after_finishing_stay_in_the_finishing_stage(self) -> None:
        (reading,) = self.parser.readings("Generated [█] 100%", 2134.7)
        self.assertEqual((reading.stage, reading.phase, reading.begins), ("finishing", "Generated", False))

    def test_other_lines_are_not_readings(self) -> None:
        for text in ("Models directory: /models", "Wrote: out.mov", "Generation timing:", "  Total generation time (including model loading): 2132.82 s", "  Sampling step time (40 step(s)): avg 51.50 s, median 54.42 s", "I1005 18:13:58.720856 10976635 ev_poll_posix.cc:587] FD from fork parent", ""):
            self.assertEqual(self.parser.readings(text, 1.0), (), text)

    def test_a_line_is_never_held_back_waiting_for_its_terminator(self) -> None:
        self.assertFalse(self.parser.accepts("Sampling... 3 / 40 [ ] 6  %"))


class H3ParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = H3StageParser()

    def test_a_phase_and_its_counter_are_a_reading_of_the_number_done(self) -> None:
        self.assertEqual(self.parser.readings("tokenizer 1/1", 0.2), (Reading("tokenize", "tokenizer", 0.2, 1, 1),))
        self.assertEqual(self.parser.readings("video VAE encoder 8/9", 1.5), (Reading("video_encode", "video VAE encoder", 1.5, 8, 9),))
        self.assertEqual(self.parser.readings("denoise 12/30", 900.0), (Reading("denoise", "denoise", 900.0, 12, 30, "steps"),))
        self.assertEqual(self.parser.readings("FFmpeg 100/243", 300.0), (Reading("ffmpeg", "FFmpeg", 300.0, 100, 243, "frames"),))

    def test_the_enqueue_counter_is_the_denoise_steps_counter_on_the_gpu_path(self) -> None:
        # h3_dit.c's GPU Euler loop reports `denoise enqueue` as each step completes and `denoise` once, at the end.
        self.assertEqual(self.parser.readings("denoise enqueue 29/30", 2100.0), (Reading("denoise", "denoise enqueue", 2100.0, 29, 30, "steps"),))

    def test_every_known_phase_has_its_stage(self) -> None:
        phases = {"tokenizer": "tokenize", "video VAE encoder": "video_encode", "Qwen vision": "vision", "text encoder": "text_encode", "refine text": "refine_text", "precompute AdaLN": "adaln", "load transformer core": "load_core", "denoise enqueue": "denoise", "denoise": "denoise", "audio VAE encoder": "audio_encode", "audio VAE": "audio_decode", "preview VAE load": "preview_load", "video VAE load": "video_load", "FFmpeg": "ffmpeg"}
        for phase, stage in phases.items():
            (reading,) = self.parser.readings(f"{phase} 1/2", 1.0)
            self.assertEqual((reading.stage, reading.phase), (stage, phase))

    def test_a_phase_the_table_does_not_know_is_kept_under_its_own_name_as_other(self) -> None:
        self.assertEqual(self.parser.readings("warm cache 2/5", 3.0), (Reading("other", "warm cache", 3.0, 2, 5),))

    def test_a_line_as_h3_writes_it_pads_the_phase_and_the_counter(self) -> None:
        # h3.c: fprintf(stderr, "\r%-25s %4d/%-4d", phase, completed, total); the runner strips the trailing blanks.
        for text, expected in (
            ("tokenizer                    1/1", ("tokenize", "tokenizer", 1, 1)),
            ("denoise enqueue             29/30", ("denoise", "denoise enqueue", 29, 30)),
            ("FFmpeg                       0/243", ("ffmpeg", "FFmpeg", 0, 243)),
            ("load transformer core       12/50", ("load_core", "load transformer core", 12, 50)),
        ):
            (reading,) = self.parser.readings(text, 1.0)
            self.assertEqual((reading.stage, reading.phase, reading.done, reading.total), expected, text)
            self.assertTrue(self.parser.accepts(text + "  "), text)

    def test_a_phase_that_has_only_started_reads_as_none_done(self) -> None:
        (reading,) = self.parser.readings("denoise                       0/30", 24.0)
        self.assertEqual((reading.done, reading.total, reading.unit), (0, 30, "steps"))

    def test_the_lines_h3_prefixes_with_its_name_are_never_phases(self) -> None:
        # h3.c's preview and frame lines also hold an n/N.
        for text in ("h3: preview 3/30", "h3: denoise preview 3/30, video frame 1/24 via kitty", "h3: frame 5/24 via iterm2", "h3: cannot write frame 3 to /tmp/x"):
            self.assertEqual(self.parser.readings(text, 1.0), (), text)
            self.assertFalse(self.parser.accepts(text), text)

    def test_text_after_the_counter_does_not_matter(self) -> None:
        (reading,) = self.parser.readings("denoise 3/30 [###   ] 10%", 5.0)
        self.assertEqual((reading.phase, reading.done, reading.total), ("denoise", 3, 30))

    def test_lines_without_a_phase_and_counter_are_not_readings(self) -> None:
        for text in ("", "loading weights", "[1/3] model.ckpt [=====>    ]  45% 1.2 GB/3 GB", "4/5", "denoise 7/0"):
            self.assertEqual(self.parser.readings(text, 1.0), (), text)

    def test_only_a_whole_counter_is_worth_releasing_before_its_terminator(self) -> None:
        self.assertTrue(self.parser.accepts("denoise 3/30"))
        self.assertTrue(self.parser.accepts("denoise 3/3"))
        for text in ("denoise", "denoise 3/", "denoise 3", "", "loading"):
            self.assertFalse(self.parser.accepts(text), text)
