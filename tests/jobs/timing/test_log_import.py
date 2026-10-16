"""Tests for rebuilding a run's stages from the job log it left."""

import unittest
from datetime import datetime, timedelta

from framecontrol.jobs.timing.log_import import LoggedRun, parse_job_log
from framecontrol.jobs.timing.model import StageSpan
from framecontrol.jobs.timing.parsers import DrawThingsStageParser, H3StageParser, StageParser
from tests.jobs.timing.transcripts import H3Phase, Transcript, draw_things, even, h3_r2av, h3_transcript

START = datetime(2026, 10, 15, 12, 0, 0)


def stamp(seconds: float) -> str:
    return (START + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def delivered_times(transcript: Transcript) -> list[float]:
    """When each line of an H3 transcript reached the log: a reading that is not its phase's last is delivered when the
    next redraw begins, which is when the next line was written; a last reading ends in a newline and is delivered at once."""
    parser = H3StageParser()
    written = [line.offset for line in transcript.lines]
    delivered = []
    for index, line in enumerate(transcript.lines):
        readings = parser.readings(line.text, line.offset)
        final = not readings or readings[0].done == readings[0].total
        delivered.append(written[index] if final or index + 1 == len(written) else written[index + 1])
    return delivered


def job_log(transcript: Transcript, *, lagged: bool, stamped: bool = False, run_start: float = 0.0, spawn: float = 3.0, number: int = 1, status: str = "succeeded", check_after: float = 4.0, delivered: list[float] | None = None) -> str:
    """The job log a run of ``transcript`` would leave: the child's lines at the offsets they were logged with (lagged
    for H3's redraws), around the app's own lines. The runner starts ``spawn`` seconds before it logs `Started subprocess`."""
    runner_start = run_start + 0.002
    times = delivered if delivered is not None else (delivered_times(transcript) if lagged else [line.offset for line in transcript.lines])
    lines = [f"{stamp(run_start - 1.0)} [app] Job walk (i2av, execution E0099): 1 runs, seed 7 (from random), cooldown off", f"{stamp(run_start)} [app] Run {number}/1 (pair p): input=<path> output=<path>"]
    marker = ", output stamped as read" if stamped else ""
    lines.append(f"{stamp(runner_start + spawn)} [app] Started subprocess (PID 4242{marker})")
    for line, offset in zip(transcript.lines, times, strict=True):
        # A line stamped as read is logged when it is delivered: at once if it ended with a newline, or an idle window
        # (0.2 s) after it was written if it was released early.
        logged = offset + (delivery_delay(line.text) if stamped else 0.0)
        lines.append(f"{stamp(runner_start + logged)} [{line.stream} +{offset:.1f}s] {line.text}")
    finished = runner_start + transcript.exit_at
    if status != "crashed":
        lines.append(f"{stamp(finished + 0.001)} [app] Subprocess finished with exit code {0 if status == 'succeeded' else 143}")
    if status == "succeeded":
        lines.append(f"{stamp(finished + 0.5)} [app] Tagged out.mp4 as BT.709 primaries")
        lines.append(f"{stamp(finished + check_after)} [app] Output check (run {number}): out.mp4: a video: ok")
    lines.append(f"{stamp(finished + check_after + 0.2)} [app] Job walk {status}: 1/1 runs completed")
    return "\n".join(lines) + "\n"


def delivery_delay(text: str) -> float:
    """How long after it was written a line stamped as read reaches the log: a reading that is not its phase's last is
    released after the idle window, and any other line is delivered at once."""
    readings = H3StageParser().readings(text.strip(), 0.0)
    return 0.21 if readings and readings[0].done != readings[0].total else 0.02


def parsers(parser: StageParser | None):
    return lambda number: parser


def assert_spans_close(test: unittest.TestCase, actual: tuple[StageSpan, ...], expected: tuple[StageSpan, ...], shift: float, tolerance: float) -> None:
    test.assertEqual([span.stage for span in actual], [span.stage for span in expected])
    for got, want in zip(actual, expected, strict=True):
        test.assertAlmostEqual(got.start, want.start + shift, delta=tolerance, msg=f"{want.stage} start")
        test.assertAlmostEqual(got.end, want.end + shift, delta=tolerance, msg=f"{want.stage} end")
        test.assertEqual((got.done, got.total, got.unit), (want.done, want.total, want.unit), want.stage)
        for index, ((offset, done), (wanted_offset, wanted_done)) in enumerate(zip(got.marks, want.marks, strict=True)):
            test.assertEqual(done, wanted_done)
            # The first mark of a stage the log marks uncertain is an estimate, as it says: only its place in order is checked.
            if index == 0 and got.uncertain:
                continue
            test.assertAlmostEqual(offset, wanted_offset + shift, delta=tolerance, msg=f"{want.stage} mark {done}")


def child_spans(run: LoggedRun) -> tuple[StageSpan, ...]:
    return tuple(span for span in run.spans if span.source == "child")


class DrawThingsLogTests(unittest.TestCase):
    def test_a_draw_things_log_gives_the_spans_its_lines_describe(self) -> None:
        transcript = draw_things()
        runs = parse_job_log(job_log(transcript, lagged=False), parsers(DrawThingsStageParser()))
        run = runs[1]
        assert_spans_close(self, child_spans(run), transcript.spans, shift=0.002, tolerance=0.2)

    def test_the_time_before_the_child_and_the_finishing_after_it_are_framecontrols(self) -> None:
        transcript = draw_things()
        run = parse_job_log(job_log(transcript, lagged=False, check_after=4.0), parsers(DrawThingsStageParser()))[1]
        prepare, *_rest, finalize = run.spans
        self.assertEqual((prepare.stage, prepare.source, prepare.start), ("prepare", "framecontrol", 0.0))
        self.assertAlmostEqual(prepare.end, 0.002, delta=0.1)
        self.assertEqual((finalize.stage, finalize.source), ("finalize", "framecontrol"))
        self.assertAlmostEqual(finalize.start, transcript.exit_at, delta=0.2)
        self.assertAlmostEqual(finalize.end, transcript.exit_at + 4.0, delta=0.2)
        self.assertAlmostEqual(run.seconds, transcript.exit_at + 4.0, delta=0.2)

    def test_lines_that_are_not_readings_do_not_make_spans(self) -> None:
        transcript = draw_things()
        run = parse_job_log(job_log(transcript, lagged=False), parsers(DrawThingsStageParser()))[1]
        self.assertEqual([span.stage for span in child_spans(run)], ["starting", "processing", "sampling", "finishing"])


class H3LogTests(unittest.TestCase):
    def test_a_lagged_log_gives_spans_within_half_a_second_of_the_ones_it_was_written_from(self) -> None:
        transcript = h3_r2av()
        run = parse_job_log(job_log(transcript, lagged=True), parsers(H3StageParser()))[1]
        assert_spans_close(self, child_spans(run), transcript.spans, shift=0.002, tolerance=0.5)

    def test_without_the_shift_the_same_log_would_be_wrong_by_a_step(self) -> None:
        transcript = h3_r2av()
        shifted = child_spans(parse_job_log(job_log(transcript, lagged=True), parsers(H3StageParser()))[1])
        denoise = next(span for span in shifted if span.stage == "denoise")
        # Step 2 was written at ~48 s, and was logged when step 3 began: the shift puts it back.
        written = next(offset for offset, done in transcript.spans[7].marks if done == 2)
        logged = next(line.offset for line in transcript.lines if line.text.split() == ["denoise", "enqueue", "3/30"])
        self.assertAlmostEqual(next(offset for offset, done in denoise.marks if done == 2), written + 0.002, delta=0.5)
        self.assertGreater(logged - written, 5.0)

    def test_the_first_reading_of_a_phase_that_cannot_be_recovered_is_marked_uncertain(self) -> None:
        run = parse_job_log(job_log(h3_r2av(), lagged=True), parsers(H3StageParser()))[1]
        flags = {span.stage: span.uncertain for span in child_spans(run) if span.marks}
        # Each follows a phase's exact last reading, so the write time of its first reading is not in the log.
        self.assertEqual(flags, {"denoise": True, "ffmpeg": True})

    def test_a_log_stamped_as_read_is_not_shifted(self) -> None:
        transcript = h3_r2av()
        run = parse_job_log(job_log(transcript, lagged=False, stamped=True), parsers(H3StageParser()))[1]
        # Most lines were logged an idle window late, so the child's origin must come from the ones logged at once.
        assert_spans_close(self, child_spans(run), transcript.spans, shift=0.002, tolerance=0.1)
        self.assertFalse(any(span.uncertain for span in run.spans))
        self.assertTrue(run.source_stamped)

    def test_a_phase_of_two_readings_puts_its_first_between_the_phase_before_and_its_end(self) -> None:
        transcript = h3_transcript((H3Phase("tokenizer", (0.2,)), H3Phase("precompute AdaLN", (10.0, 20.0)), H3Phase("denoise", even(21.0, 30.0, 4))), 31.0)
        run = parse_job_log(job_log(transcript, lagged=True), parsers(H3StageParser()))[1]
        adaln = next(span for span in child_spans(run) if span.stage == "adaln")
        self.assertAlmostEqual(adaln.end, 20.002, delta=0.2)

    def test_a_run_interrupted_mid_phase_keeps_the_readings_it_had(self) -> None:
        transcript = h3_transcript((H3Phase("tokenizer", (0.2,)), H3Phase("denoise", even(10.0, 100.0, 10))), 55.0)
        cut = Transcript(tuple(line for line in transcript.lines if line.offset <= 55.0), transcript.spans, 55.0)
        log = job_log(cut, lagged=True, status="interrupted")
        run = parse_job_log(log, parsers(H3StageParser()))[1]
        denoise = child_spans(run)[-1]
        self.assertEqual((denoise.stage, denoise.done, denoise.total), ("denoise", 5, 10))
        self.assertAlmostEqual(denoise.end, 55.0, delta=1.0)
        self.assertEqual((run.finished, run.exit_code), (True, 143))
        self.assertNotIn("finalize", [span.stage for span in run.spans])

    def test_a_log_that_ends_with_the_child_still_running_ends_its_last_span_at_the_last_line(self) -> None:
        transcript = h3_transcript((H3Phase("tokenizer", (0.2,)), H3Phase("denoise", even(10.0, 100.0, 10))), 55.0)
        cut = Transcript(tuple(line for line in transcript.lines if line.offset <= 55.0), transcript.spans, 55.0)
        run = parse_job_log(job_log(cut, lagged=True, status="crashed"), parsers(H3StageParser()))[1]
        self.assertFalse(run.finished)
        self.assertIsNone(run.exit_code)
        self.assertAlmostEqual(child_spans(run)[-1].end, 50.0, delta=1.0)


class LogShapeTests(unittest.TestCase):
    def test_each_run_of_a_log_is_read_on_its_own_from_its_run_line(self) -> None:
        first = job_log(draw_things(), lagged=False)
        second = job_log(draw_things(), lagged=False, run_start=4000.0, number=2)
        text = first + "\n".join(line for line in second.splitlines() if "Job walk" not in line) + "\n"
        runs = parse_job_log(text, parsers(DrawThingsStageParser()))
        self.assertEqual(sorted(runs), [1, 2])
        self.assertEqual([span.stage for span in child_spans(runs[2])], ["starting", "processing", "sampling", "finishing"])

    def test_lines_that_are_not_log_lines_are_ignored(self) -> None:
        text = "Traceback (most recent call last):\n  File x\n" + job_log(draw_things(), lagged=False) + "\nValueError: boom\n"
        self.assertEqual(sorted(parse_job_log(text, parsers(DrawThingsStageParser()))), [1])

    def test_a_backend_with_no_parser_gets_the_child_as_one_span(self) -> None:
        run = parse_job_log(job_log(draw_things(), lagged=False), parsers(None))[1]
        self.assertEqual([span.stage for span in child_spans(run)], ["child"])

    def test_a_log_with_no_run_gives_nothing(self) -> None:
        self.assertEqual(parse_job_log("", parsers(None)), {})
