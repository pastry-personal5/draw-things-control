"""Tests for a run's timeline: the child's stages and Framecontrol's own on one clock, with no gap hidden."""

import unittest

from framecontrol.jobs.timing.model import StageSpan
from framecontrol.jobs.timing.parsers import DrawThingsStageParser, H3StageParser
from framecontrol.jobs.timing.timeline import NO_TIMER, RunTimeline
from tests.jobs.timing.transcripts import FakeClock, Transcript, draw_things, h3_i2av, h3_r2av


class Recorder:
    def __init__(self) -> None:
        self.started: list[StageSpan] = []
        self.finished: list[StageSpan] = []


def timeline_of(clock: FakeClock, parser: H3StageParser | DrawThingsStageParser | None, recorder: Recorder) -> RunTimeline:
    return RunTimeline(clock, parser=parser, on_started=recorder.started.append, on_finished=recorder.finished.append)


def play(timeline: RunTimeline, clock: FakeClock, transcript: Transcript, *, spawn_after: float = 0.05) -> None:
    timeline.begin()
    clock.now += spawn_after
    timeline.child_started()
    child_origin = clock.now
    for line in transcript.lines:
        timeline.reading(line.text, line.offset)
    clock.now = child_origin + transcript.exit_at
    timeline.child_finished(transcript.exit_at)


class RunTimelineTests(unittest.TestCase):
    def test_the_stages_add_up_to_the_runs_time_with_the_gaps_between_them_named(self) -> None:
        clock, recorder = FakeClock(), Recorder()
        timeline = timeline_of(clock, H3StageParser(), recorder)
        play(timeline, clock, h3_r2av())
        with timeline.stage("verify"):
            clock.now += 1.5
        clock.now += 0.2
        with timeline.stage("correct"):
            clock.now += 40.0
        clock.now += 0.7
        spans = timeline.finish()
        elapsed = clock.now - 1000.0
        self.assertAlmostEqual(sum(span.end - span.start for span in spans), elapsed, places=9)
        self.assertEqual([(span.stage, round(span.end - span.start, 3)) for span in spans if span.stage in {"verify", "correct", "unaccounted"}], [("verify", 1.5), ("unaccounted", 0.2), ("correct", 40.0), ("unaccounted", 0.7)])
        self.assertEqual([span.source for span in spans if span.stage in {"prepare", "verify"}], ["framecontrol", "framecontrol"])

    def test_an_i2av_run_with_a_color_correction_tail_adds_up_and_covers_its_child(self) -> None:
        clock, recorder = FakeClock(), Recorder()
        timeline = timeline_of(clock, H3StageParser(), recorder)
        transcript = h3_i2av()
        play(timeline, clock, transcript, spawn_after=0.1)
        for stage, seconds in (("verify", 1.4), ("copy_raw", 0.9), ("convert", 31.0), ("drift", 4.2), ("correct", 38.0), ("last_frame", 0.8), ("promote", 0.05)):
            with timeline.stage(stage):
                clock.now += seconds
            clock.now += 0.004
        spans = timeline.finish()
        elapsed = clock.now - 1000.0
        self.assertAlmostEqual(sum(span.end - span.start for span in spans), elapsed, delta=0.1)
        child = [span for span in spans if span.source == "child"]
        self.assertAlmostEqual(child[0].start, 0.1)
        self.assertAlmostEqual(child[-1].end - child[0].start, transcript.exit_at, delta=1.0)
        self.assertEqual([span.stage for span in spans if span.source == "framecontrol" and span.stage not in {"prepare", "unaccounted"}], ["verify", "copy_raw", "convert", "drift", "correct", "last_frame", "promote"])
        self.assertLess(sum(span.end - span.start for span in spans if span.stage == "unaccounted"), 0.1)

    def test_prepare_runs_from_the_start_of_the_run_to_the_child_spawn(self) -> None:
        clock, recorder = FakeClock(), Recorder()
        timeline = timeline_of(clock, H3StageParser(), recorder)
        play(timeline, clock, h3_r2av(), spawn_after=0.25)
        spans = timeline.finish()
        self.assertEqual((spans[0].stage, spans[0].start, spans[0].end), ("prepare", 0.0, 0.25))
        self.assertEqual((spans[1].stage, spans[1].start), ("tokenize", 0.25))

    def test_child_spans_sit_at_the_childs_place_on_the_run_clock(self) -> None:
        clock, recorder = FakeClock(), Recorder()
        timeline = timeline_of(clock, DrawThingsStageParser(), recorder)
        transcript = draw_things()
        play(timeline, clock, transcript, spawn_after=0.5)
        spans = {span.stage: span for span in timeline.finish()}
        self.assertEqual(spans["starting"].start, 0.5)
        self.assertAlmostEqual(spans["processing"].start, 0.5 + 9.1)
        self.assertAlmostEqual(spans["finishing"].end, 0.5 + transcript.exit_at)
        self.assertAlmostEqual(spans["sampling"].marks[0][0], 0.5 + 17.1)

    def test_a_run_that_never_started_a_child_is_all_prepare_and_what_follows(self) -> None:
        clock, recorder = FakeClock(), Recorder()
        timeline = timeline_of(clock, H3StageParser(), recorder)
        timeline.begin()
        clock.now += 0.3
        spans = timeline.finish()
        self.assertEqual([(span.stage, span.start, round(span.end, 9)) for span in spans], [("prepare", 0.0, 0.3)])

    def test_a_child_killed_with_no_exit_reported_ends_with_the_run(self) -> None:
        clock, recorder = FakeClock(), Recorder()
        timeline = timeline_of(clock, H3StageParser(), recorder)
        timeline.begin()
        clock.now += 0.1
        timeline.child_started()
        timeline.reading("tokenizer 1/1", 0.2)
        clock.now += 5.0
        spans = timeline.finish()
        self.assertAlmostEqual(spans[-1].end, 5.1)
        self.assertAlmostEqual(sum(span.end - span.start for span in spans), 5.1)

    def test_a_stage_that_raises_is_still_closed(self) -> None:
        clock, recorder = FakeClock(), Recorder()
        timeline = timeline_of(clock, None, recorder)
        timeline.begin()
        with self.assertRaises(ValueError), timeline.stage("verify"):
            clock.now += 2.0
            raise ValueError("bad")
        self.assertEqual([(span.stage, span.end - span.start) for span in recorder.finished], [("verify", 2.0)])

    def test_work_done_before_the_run_started_has_negative_offsets_and_is_not_in_the_sum(self) -> None:
        clock, recorder = FakeClock(), Recorder()
        timeline = timeline_of(clock, None, recorder)
        timeline.begin()
        timeline.before_start("prepare_input", 996.0, 998.5)
        clock.now += 1.0
        spans = timeline.finish()
        before = [span for span in spans if span.start < 0]
        self.assertEqual([(span.stage, span.source, span.start, span.end) for span in before], [("prepare_input", "framecontrol", -4.0, -1.5)])
        self.assertAlmostEqual(sum(span.end - span.start for span in spans if span.start >= 0), 1.0)

    def test_an_observer_hears_every_span_begin_and_end_including_the_gaps(self) -> None:
        clock, recorder = FakeClock(), Recorder()
        timeline = timeline_of(clock, None, recorder)
        timeline.begin()
        clock.now += 0.1
        timeline.child_started()
        timeline.child_finished(1.0)
        clock.now += 1.0
        clock.now += 0.5
        with timeline.stage("tag"):
            clock.now += 0.25
        timeline.finish()
        self.assertEqual([span.stage for span in recorder.started][:3], ["prepare", "child", "tag"])
        self.assertEqual(sorted(span.stage for span in recorder.started), sorted(span.stage for span in recorder.finished))
        self.assertIn("unaccounted", [span.stage for span in recorder.finished])

    def test_the_reading_a_line_gave_is_returned_whole(self) -> None:
        clock, recorder = FakeClock(), Recorder()
        timeline = timeline_of(clock, H3StageParser(), recorder)
        timeline.begin()
        timeline.child_started()
        reading = timeline.reading("denoise 3/30", 100.0)
        assert reading is not None
        self.assertEqual((reading.stage, reading.phase, reading.done, reading.total), ("denoise", "denoise", 3, 30))
        self.assertIsNone(timeline.reading("loading", 101.0))

    def test_without_a_timeline_a_timer_times_nothing(self) -> None:
        with NO_TIMER.stage("verify"):
            pass
