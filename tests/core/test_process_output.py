import sys
import unittest

from draw_things_control.core.draw_things_runner import DrawThingsProcessRunner
from draw_things_control.core.process_output import OutputProcessor, OutputStream, strip_terminal_codes
from tests.core.test_runner import ProcessCommand

# What draw-things-cli prints through a pipe: a bare newline once, then cursor-up + clear-line before every bar line.
CLEAR = "\x1b[1A\x1b[K"


class ProgressBarOutputTests(unittest.TestCase):
    def process(self, *lines: str) -> OutputProcessor:
        processor = OutputProcessor()
        for line in lines:
            processor.process(OutputStream.STDOUT, line, 0.1)
        return processor

    def test_bar_line_loses_its_escape_codes_and_keeps_step_and_percent(self) -> None:
        (message,) = self.process(f"{CLEAR}Sampling... 3 / 20 [█] 15%\n").messages
        self.assertEqual(message.text, "Sampling... 3 / 20 [█] 15%")
        self.assertEqual(message.progress, (3, 20))
        self.assertEqual(message.percent, 15)

    def test_stage_without_a_step_counter_still_reports_its_percent(self) -> None:
        (message,) = self.process(f"{CLEAR}Processing... [ ]  10%\n").messages
        self.assertEqual((message.progress, message.percent), (None, 10))
        (done,) = self.process(f"{CLEAR}Generated [█] 100%\n").messages
        self.assertEqual((done.progress, done.percent), (None, 100))

    def test_the_bare_newline_before_the_first_bar_is_dropped(self) -> None:
        processor = self.process("\n", f"{CLEAR}Starting... [ ]   0%\n", CLEAR + "\n")
        self.assertEqual([message.text for message in processor.messages], ["Starting... [ ]   0%"])

    def test_download_line_reports_percent_but_not_its_file_counter_as_steps(self) -> None:
        (message,) = self.process("\r[1/3] model.ckpt [=====>    ]  45% 1.2 GB/3 GB").messages
        self.assertEqual((message.progress, message.percent), (None, 45))

    def test_other_escape_sequences_are_removed(self) -> None:
        self.assertEqual(strip_terminal_codes("\x1b[31mred\x1b[0m \x1b]1337;File=inline=1:AAAA\x07after \x1b_Gf=100;AAAA\x1b\\end"), "red after end")

    def test_plain_text_is_unchanged_and_out_of_range_percent_is_ignored(self) -> None:
        (message,) = self.process("warning: 250% of something\n").messages
        self.assertEqual((message.text, message.percent), ("warning: 250% of something", None))

    def test_runner_reads_a_bar_written_by_a_child_process(self) -> None:
        script = "import sys\nsys.stdout.write('\\n')\nfor n in (1, 2):\n    sys.stdout.write('\\x1b[1A\\x1b[KSampling... %d / 2 [█] %d%%\\n' % (n, n * 50))\nsys.stdout.write('\\x1b[1A\\x1b[KGenerated [█] 100%\\n')\nsys.stdout.flush()"
        result = DrawThingsProcessRunner(ProcessCommand((sys.executable, "-c", script)), output_processor=OutputProcessor(), handle_signals=False).run()
        self.assertTrue(result.succeeded)
        self.assertEqual([(message.text, message.progress, message.percent) for message in result.messages], [("Sampling... 1 / 2 [█] 50%", (1, 2), 50), ("Sampling... 2 / 2 [█] 100%", (2, 2), 100), ("Generated [█] 100%", None, 100)])


if __name__ == "__main__":
    unittest.main()
