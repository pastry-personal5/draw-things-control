"""The draw-things-cli pane's output: every line draw-things-cli prints reaches it, progress lines included."""

from __future__ import annotations

import unittest

from draw_things_control.jobs.events import RunOutput
from draw_things_control.tui.live_run import LiveRun


def line(text: str, *, progress: tuple[int, int] | None = None, percent: int | None = None) -> RunOutput:
    return RunOutput(at="", number=1, stream="stdout", text=text, progress=progress, percent=percent)


class OutputLinesTests(unittest.TestCase):
    def test_progress_lines_are_kept_as_output_and_still_update_the_progress(self) -> None:
        live = LiveRun()
        for event in (line("Processing... [ ] 2  %", percent=2), line("Sampling... 2 / 40 [ ] 4  %", progress=(2, 40), percent=4), line("Saved output")):
            live.apply(event)
        self.assertEqual([entry.text for entry in live.output], ["Processing... [ ] 2  %", "Sampling... 2 / 40 [ ] 4  %", "Saved output"])
        self.assertEqual(live.output_count, 3)
        self.assertEqual((live.progress, live.percent), ((2, 40), 4))

    def test_a_seeded_progress_reading_has_no_text_and_adds_no_line(self) -> None:
        live = LiveRun()
        live.apply(line("", progress=(12, 40)))
        self.assertEqual(live.output_count, 0)
        self.assertEqual(live.progress, (12, 40))
