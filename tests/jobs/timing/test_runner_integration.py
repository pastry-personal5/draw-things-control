"""The real process runner feeding a real run timeline from a child that redraws as H3 does: no fakes between the pipe and the spans."""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from framecontrol.core.process.output import OutputProcessor, ProcessMessage
from framecontrol.core.process.runner import DrawThingsProcessRunner
from framecontrol.jobs.timing.parsers import H3StageParser
from framecontrol.jobs.timing.timeline import RunTimeline
from tests.core.test_runner import RedrawCommand

# Redraws each reading with a leading carriage return and ends only a phase's last, noting when it wrote.
CHILD = "import json, os, sys, time\nlog = open(sys.argv[1], 'w')\nfor text, last, pause in json.loads(sys.argv[2]):\n    os.write(1, ('\\r' + text + ('\\n' if last else '')).encode())\n    log.write(json.dumps([text, time.monotonic()]) + '\\n')\n    log.flush()\n    time.sleep(pause)\n"


class RunnerToTimelineTests(unittest.TestCase):
    def test_a_child_that_redraws_without_terminators_gives_the_spans_of_when_it_wrote(self) -> None:
        plan = [["tokenizer 1/1", True, 0.3], ["text encoder 1/3", False, 0.3], ["text encoder 2/3", False, 0.3], ["text encoder 3/3", True, 0.3], ["denoise 1/2", False, 0.4], ["denoise 2/2", True, 0.3], ["FFmpeg 1/2", False, 0.3], ["FFmpeg 2/2", True, 0.3]]
        timeline = RunTimeline(time.monotonic, parser=H3StageParser())

        def on_message(message: ProcessMessage) -> None:
            timeline.reading(message.text, message.elapsed_seconds)

        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "writes"
            command = RedrawCommand((sys.executable, "-c", CHILD, str(log), json.dumps(plan)), H3StageParser().accepts)
            timeline.begin()
            timeline.child_started()
            DrawThingsProcessRunner(command, output_processor=OutputProcessor(callback=on_message), handle_signals=False).run()
            timeline.child_finished()
            written = dict(json.loads(line) for line in log.read_text(encoding="utf-8").splitlines())
        spans = {span.stage: span for span in timeline.finish() if span.source == "child"}
        self.assertEqual(list(spans), ["tokenize", "text_encode", "denoise", "ffmpeg"])
        # The clocks differ by a constant: compare each stage's end with the write of its last reading, from the first's.
        base = spans["tokenize"].end - written["tokenizer 1/1"]
        for stage, last in (("tokenize", "tokenizer 1/1"), ("text_encode", "text encoder 3/3"), ("denoise", "denoise 2/2")):
            self.assertAlmostEqual(spans[stage].end - written[last], base, delta=0.08, msg=stage)
        denoise = spans["denoise"]
        self.assertEqual([done for _offset, done in denoise.marks], [1, 2])
        self.assertAlmostEqual(denoise.marks[0][0] - written["denoise 1/2"], base, delta=0.08)
        self.assertEqual(spans["text_encode"].marks, ())
