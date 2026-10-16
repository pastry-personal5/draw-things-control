"""Tests for turning readings into spans."""

import unittest

from framecontrol.jobs.timing.model import Reading, StageSpan, thin_marks
from framecontrol.jobs.timing.parsers import DrawThingsStageParser, H3StageParser, StageParser
from framecontrol.jobs.timing.tracker import StageTracker
from tests.jobs.timing.transcripts import DrawThingsRun, Transcript, draw_things, h3_r2av


def track(transcript: Transcript, parser: StageParser, tracker: StageTracker | None = None) -> tuple[StageSpan, ...]:
    tracker = tracker or StageTracker()
    for line in transcript.lines:
        for reading in parser.readings(line.text, line.offset):
            tracker.feed(reading)
    tracker.finish(transcript.exit_at)
    return tracker.spans


class TrackerOnTranscriptTests(unittest.TestCase):
    def test_an_h3_run_becomes_the_spans_it_was_built_from(self) -> None:
        transcript = h3_r2av()
        self.assertEqual(track(transcript, H3StageParser()), transcript.spans)

    def test_a_draw_things_run_becomes_the_spans_it_was_built_from(self) -> None:
        transcript = draw_things()
        self.assertEqual(track(transcript, DrawThingsStageParser()), transcript.spans)

    def test_spans_cover_the_child_from_its_start_to_its_exit_with_no_gap_or_overlap(self) -> None:
        for transcript, parser in ((h3_r2av(), H3StageParser()), (draw_things(), DrawThingsStageParser())):
            spans = track(transcript, parser)
            self.assertEqual(spans[0].start, 0.0)
            self.assertEqual(spans[-1].end, transcript.exit_at)
            for earlier, later in zip(spans, spans[1:], strict=False):
                self.assertEqual(earlier.end, later.start)

    def test_the_last_draw_things_step_is_complete_when_finishing_begins(self) -> None:
        sampling = next(span for span in track(draw_things(), DrawThingsStageParser()) if span.stage == "sampling")
        self.assertEqual((sampling.done, sampling.total, sampling.unit), (40, 40, "steps"))
        self.assertEqual(sampling.marks[0], (17.1, 0))
        self.assertEqual(sampling.marks[-1], (2123.0, 40))

    def test_a_run_killed_during_sampling_keeps_the_steps_it_finished(self) -> None:
        run = DrawThingsRun()
        transcript = draw_things(run)
        cut = [line for line in transcript.lines if line.offset <= run.step_starts[11] and line.text.startswith(("Starting", "Processing", "Sampling"))]
        tracker = StageTracker()
        parser = DrawThingsStageParser()
        for line in cut:
            for reading in parser.readings(line.text, line.offset):
                tracker.feed(reading)
        tracker.finish(run.step_starts[11] + 30.0)
        sampling = tracker.spans[-1]
        self.assertEqual((sampling.stage, sampling.done, sampling.total), ("sampling", 11, 40))
        self.assertEqual(sampling.end, run.step_starts[11] + 30.0)

    def test_every_h3_reading_is_in_a_span(self) -> None:
        transcript = h3_r2av()
        spans = track(transcript, H3StageParser())
        for line in transcript.lines:
            self.assertTrue(any(span.start <= line.offset <= span.end for span in spans), line)


class DrawThingsStatisticsTests(unittest.TestCase):
    """The run's total and its step times, read from the spans, against what the CLI's own ``Generation timing:`` block says."""

    def setUp(self) -> None:
        self.transcript = draw_things()
        self.spans = {span.stage: span for span in track(self.transcript, DrawThingsStageParser())}

    def test_the_total_agrees_with_the_cli_within_one_percent(self) -> None:
        total = self.spans["finishing"].end
        assert self.transcript.stated_total is not None
        self.assertAlmostEqual(total, self.transcript.stated_total, delta=0.01 * self.transcript.stated_total)

    def test_the_average_and_median_step_agree_by_the_clis_definition(self) -> None:
        # 40 durations: the first from the last Processing line to the first sampling reading, and the 39 gaps between readings.
        processing = [line.offset for line in self.transcript.lines if line.text.startswith("Processing")]
        readings = [offset for offset, _done in self.spans["sampling"].marks[:-1]]
        durations = [readings[0] - processing[-1]] + [later - earlier for earlier, later in zip(readings, readings[1:], strict=False)]
        self.assertEqual(len(durations), 40)
        ordered = sorted(durations)
        median = (ordered[19] + ordered[20]) / 2
        average = sum(durations) / len(durations)
        assert self.transcript.stated_average is not None and self.transcript.stated_median is not None
        self.assertAlmostEqual(average, self.transcript.stated_average, delta=0.01 * self.transcript.stated_average)
        self.assertAlmostEqual(median, self.transcript.stated_median, delta=0.01 * self.transcript.stated_median)

    def test_the_last_step_is_not_counted_in_that_definition_but_the_marks_still_hold_it(self) -> None:
        marks = self.spans["sampling"].marks
        self.assertEqual(len(marks), 41)
        self.assertEqual([done for _offset, done in marks[:3]], [0, 1, 2])
        self.assertEqual(marks[-1][1], 40)
        self.assertGreater(marks[-1][0] - marks[-2][0], 40.0)


class TrackerRuleTests(unittest.TestCase):
    def test_the_enqueue_counter_adds_no_marks_and_does_not_make_the_step_counter_go_back(self) -> None:
        tracker = StageTracker()
        for reading in (Reading("denoise", "denoise enqueue", 1.0, 3, 30, "steps", counted=False), Reading("denoise", "denoise", 2.0, 1, 30, "steps"), Reading("denoise", "denoise enqueue", 2.5, 4, 30, "steps", counted=False), Reading("denoise", "denoise", 3.0, 2, 30, "steps")):
            tracker.feed(reading)
        tracker.finish(4.0)
        (span,) = tracker.spans
        self.assertEqual((span.done, span.marks), (2, ((2.0, 1), (3.0, 2))))

    def test_a_counter_that_goes_back_opens_a_new_span_instead_of_rewinding(self) -> None:
        tracker = StageTracker()
        for offset, done in ((10.0, 0), (20.0, 1), (30.0, 2), (40.0, 0), (50.0, 1)):
            tracker.feed(Reading("sampling", "Sampling", offset, done, 3, "steps", begins=True))
        tracker.finish(60.0)
        self.assertEqual([(span.start, span.end, span.done) for span in tracker.spans], [(0.0, 40.0, 2), (40.0, 60.0, 1)])

    def test_a_phase_that_returns_with_a_new_total_opens_a_new_span(self) -> None:
        tracker = StageTracker()
        for offset, done, total in ((1.0, 1, 2), (2.0, 2, 2), (3.0, 1, 4), (4.0, 4, 4)):
            tracker.feed(Reading("text_encode", "text encoder", offset, done, total))
        tracker.finish(5.0)
        self.assertEqual([(span.start, span.end, span.total) for span in tracker.spans], [(0.0, 2.0, 2), (2.0, 5.0, 4)])

    def test_an_unknown_phase_keeps_its_own_span_and_name(self) -> None:
        tracker = StageTracker()
        for reading in (Reading("tokenize", "tokenizer", 0.5, 1, 1), Reading("other", "warm cache", 1.0, 1, 2), Reading("other", "warm cache", 2.0, 2, 2), Reading("other", "cool cache", 3.0, 1, 1)):
            tracker.feed(reading)
        tracker.finish(4.0)
        self.assertEqual([(span.stage, span.phase) for span in tracker.spans], [("tokenize", "tokenizer"), ("other", "warm cache"), ("other", "cool cache")])

    def test_a_child_that_printed_no_reading_is_one_span_of_its_whole_time(self) -> None:
        tracker = StageTracker()
        tracker.finish(0.8)
        self.assertEqual(tracker.spans, (StageSpan("child", None, "child", 0.0, 0.8),))

    def test_observers_hear_a_span_when_it_opens_and_when_it_closes(self) -> None:
        heard: list[tuple[str, str, float]] = []
        tracker = StageTracker(on_started=lambda span: heard.append(("start", span.stage, span.start)), on_finished=lambda span: heard.append(("finish", span.stage, span.end)))
        tracker.feed(Reading("tokenize", "tokenizer", 0.2, 1, 1))
        tracker.feed(Reading("video_encode", "video VAE encoder", 0.4, 1, 9))
        tracker.finish(1.0)
        self.assertEqual(heard, [("start", "tokenize", 0.0), ("finish", "tokenize", 0.2), ("start", "video_encode", 0.2), ("finish", "video_encode", 1.0)])

    def test_the_origin_is_where_the_first_span_starts(self) -> None:
        tracker = StageTracker(origin=5.0)
        tracker.feed(Reading("tokenize", "tokenizer", 5.2, 1, 1))
        tracker.finish(6.0)
        self.assertEqual((tracker.spans[0].start, tracker.spans[0].end), (5.0, 6.0))

    def test_a_stage_with_a_unit_keeps_a_mark_for_each_reading_and_other_stages_keep_none(self) -> None:
        tracker = StageTracker()
        tracker.feed(Reading("text_encode", "text encoder", 1.0, 1, 2))
        tracker.feed(Reading("denoise", "denoise", 2.0, 1, 2, "steps"))
        tracker.finish(3.0)
        self.assertEqual([span.marks for span in tracker.spans], [(), ((2.0, 1),)])


class ThinMarksTests(unittest.TestCase):
    def test_marks_up_to_the_limit_are_kept_whole(self) -> None:
        marks = tuple((float(index), index) for index in range(1000))
        self.assertEqual(thin_marks(marks, 1000), marks)

    def test_more_marks_are_thinned_evenly_keeping_the_first_and_the_last(self) -> None:
        marks = tuple((float(index), index) for index in range(2500))
        thinned = thin_marks(marks, 1000)
        self.assertEqual(len(thinned), 1000)
        self.assertEqual((thinned[0], thinned[-1]), (marks[0], marks[-1]))
        self.assertEqual(list(thinned), sorted(thinned))
        gaps = {later[1] - earlier[1] for earlier, later in zip(thinned, thinned[1:], strict=False)}
        self.assertLessEqual(max(gaps) - min(gaps), 1)
