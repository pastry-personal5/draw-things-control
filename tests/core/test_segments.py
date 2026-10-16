"""Tests for cutting a child's byte stream into the segments it wrote, each stamped when its last byte arrived."""

import unittest

from framecontrol.core.process.segments import Segment, SegmentSplitter


def accepts_counter(text: str) -> bool:
    """A stand-in for a backend's predicate: the text ends in ``n/N``."""
    head, _, tail = text.rpartition(" ")
    return bool(head) and "/" in tail and all(part.isdigit() and part for part in tail.split("/", 1))


class SegmentSplitterTests(unittest.TestCase):
    def test_a_terminated_line_is_one_segment_stamped_when_it_arrived(self) -> None:
        splitter = SegmentSplitter()
        self.assertEqual(splitter.feed("Sampling... 1 / 4\nSampling... 2 / 4\n", 5.0), [Segment("Sampling... 1 / 4", 5.0), Segment("Sampling... 2 / 4", 5.0)])

    def test_a_carriage_return_ends_a_segment_and_a_crlf_ends_one_not_two(self) -> None:
        splitter = SegmentSplitter()
        self.assertEqual(splitter.feed("a\rb\r\nc\n", 1.0), [Segment("a", 1.0), Segment("b", 1.0), Segment("c", 1.0)])

    def test_a_reading_is_stamped_when_its_own_last_byte_arrived_not_when_the_next_redraw_ended_it(self) -> None:
        splitter = SegmentSplitter()
        self.assertEqual(splitter.feed("\rdenoise 1/30", 10.0), [])
        # The next redraw's \r and its text arrive together, 19 s later: reading 1 is delivered now but happened at 10.0.
        self.assertEqual(splitter.feed("\rdenoise 2/30", 29.0), [Segment("denoise 1/30", 10.0)])
        self.assertEqual(splitter.feed("\rdenoise 3/30", 48.0), [Segment("denoise 2/30", 29.0)])

    def test_a_segment_written_in_pieces_is_stamped_with_its_last_piece(self) -> None:
        splitter = SegmentSplitter()
        splitter.feed("\rdenoise 3/3", 1.00)
        splitter.feed("0", 1.01)
        self.assertEqual(splitter.feed("\rnext", 2.0), [Segment("denoise 3/30", 1.01)])

    def test_a_reading_nothing_follows_is_released_after_the_idle_window_with_its_own_stamp(self) -> None:
        splitter = SegmentSplitter(accepts_counter, idle_seconds=0.2)
        splitter.feed("\rdenoise 4/30", 3.0)
        self.assertEqual(splitter.idle_deadline(), 3.2)
        self.assertEqual(splitter.release_idle(3.19), [])
        self.assertEqual(splitter.release_idle(3.2), [Segment("denoise 4/30", 3.0)])
        self.assertIsNone(splitter.idle_deadline())

    def test_a_released_reading_is_not_delivered_again_when_the_redraw_that_ends_it_comes(self) -> None:
        splitter = SegmentSplitter(accepts_counter, idle_seconds=0.2)
        splitter.feed("\rdenoise 4/30", 3.0)
        self.assertEqual(splitter.release_idle(3.3), [Segment("denoise 4/30", 3.0)])
        # Its terminator is the next redraw's \r: the copy it ends was delivered already, and the next reading follows alone.
        self.assertEqual(splitter.feed("\rdenoise 5/30", 22.0), [])
        self.assertEqual(splitter.feed("\n", 40.0), [Segment("denoise 5/30", 22.0)])
        self.assertEqual(splitter.flush(), [])

    def test_a_released_reading_is_not_delivered_again_at_the_end_of_the_stream(self) -> None:
        splitter = SegmentSplitter(accepts_counter, idle_seconds=0.2)
        splitter.feed("\rdenoise 4/30", 3.0)
        splitter.release_idle(3.3)
        self.assertEqual(splitter.flush(), [])

    def test_a_released_reading_that_then_grows_is_delivered_as_the_longer_one(self) -> None:
        splitter = SegmentSplitter(accepts_counter, idle_seconds=0.2)
        splitter.feed("\rdenoise 3/3", 1.0)
        self.assertEqual(splitter.release_idle(1.3), [Segment("denoise 3/3", 1.0)])
        splitter.feed("0", 1.4)
        self.assertEqual(splitter.feed("\n", 1.5), [Segment("denoise 3/30", 1.4)])

    def test_a_write_that_splits_a_number_inside_the_window_is_never_released_half_read(self) -> None:
        splitter = SegmentSplitter(accepts_counter, idle_seconds=0.2)
        splitter.feed("\rdenoise 3/3", 1.00)
        self.assertEqual(splitter.release_idle(1.10), [])
        splitter.feed("0", 1.12)
        self.assertEqual(splitter.idle_deadline(), 1.12 + 0.2)
        self.assertEqual(splitter.release_idle(1.33), [Segment("denoise 3/30", 1.12)])

    def test_text_the_predicate_does_not_accept_waits_for_its_terminator(self) -> None:
        splitter = SegmentSplitter(accepts_counter, idle_seconds=0.2)
        splitter.feed("loading weights", 1.0)
        self.assertIsNone(splitter.idle_deadline())
        self.assertEqual(splitter.release_idle(9.0), [])
        self.assertEqual(splitter.feed("\n", 9.5), [Segment("loading weights", 1.0)])

    def test_the_predicate_sees_the_text_without_terminal_codes(self) -> None:
        splitter = SegmentSplitter(accepts_counter, idle_seconds=0.2)
        splitter.feed("\x1b[1A\x1b[Kdenoise 4/30", 3.0)
        self.assertEqual(splitter.release_idle(3.3), [Segment("\x1b[1A\x1b[Kdenoise 4/30", 3.0)])

    def test_without_a_predicate_an_unterminated_tail_waits_for_the_end_of_the_stream(self) -> None:
        splitter = SegmentSplitter()
        splitter.feed("tail", 4.0)
        self.assertIsNone(splitter.idle_deadline())
        self.assertEqual(splitter.flush(), [Segment("tail", 4.0)])
        self.assertEqual(splitter.flush(), [])

    def test_a_blank_segment_is_not_delivered(self) -> None:
        splitter = SegmentSplitter()
        self.assertEqual(splitter.feed("\n\r\n\r", 1.0), [])
