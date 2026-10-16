"""The draw-things-cli pane's output: every line draw-things-cli prints reaches it, progress lines included."""

from __future__ import annotations

import unittest

from framecontrol.jobs.events import RunOutput
from framecontrol.tui.live_run import LiveRun


def line(text: str) -> RunOutput:
    return RunOutput(at="", number=1, stream="stdout", text=text, progress=None, percent=None, phase=None, offset=0.0)


class OutputLinesTests(unittest.TestCase):
    def test_progress_lines_are_kept_as_output(self) -> None:
        live = LiveRun()
        for event in (line("Processing... [ ] 2  %"), line("Sampling... 2 / 40 [ ] 4  %"), line("Saved output")):
            live.apply(event)
        self.assertEqual([entry.text for entry in live.output], ["Processing... [ ] 2  %", "Sampling... 2 / 40 [ ] 4  %", "Saved output"])
        self.assertEqual(live.output_count, 3)
