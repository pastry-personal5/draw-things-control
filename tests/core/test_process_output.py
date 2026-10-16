import sys
import unittest

from framecontrol.core.process.output import OutputProcessor, OutputStream, strip_terminal_codes
from framecontrol.core.process.runner import DrawThingsProcessRunner
from tests.core.test_runner import ProcessCommand

# What draw-things-cli prints through a pipe: a bare newline once, then cursor-up + clear-line before every bar line.
CLEAR = "\x1b[1A\x1b[K"


class BarOutputTests(unittest.TestCase):
    def process(self, *lines: str) -> OutputProcessor:
        processor = OutputProcessor()
        for line in lines:
            processor.process(OutputStream.STDOUT, line, 0.1)
        return processor

    def test_bar_line_loses_its_escape_codes_and_keeps_its_text(self) -> None:
        (message,) = self.process(f"{CLEAR}Sampling... 3 / 20 [█] 15%\n").messages
        self.assertEqual(message.text, "Sampling... 3 / 20 [█] 15%")
        (done,) = self.process(f"{CLEAR}Generated [█] 100%\n").messages
        self.assertEqual(done.text, "Generated [█] 100%")

    def test_a_message_reads_no_step_or_percent_of_its_own(self) -> None:
        # What a line means is the backend's to say (jobs/timing/parsers.py), not a pattern for any n/N in any line.
        (message,) = self.process(f"{CLEAR}Sampling... 3 / 20 [█] 15%\n").messages
        self.assertFalse(hasattr(message, "progress") or hasattr(message, "percent"))

    def test_the_bare_newline_before_the_first_bar_is_dropped(self) -> None:
        processor = self.process("\n", f"{CLEAR}Starting... [ ]   0%\n", CLEAR + "\n")
        self.assertEqual([message.text for message in processor.messages], ["Starting... [ ]   0%"])

    def test_other_escape_sequences_are_removed(self) -> None:
        self.assertEqual(strip_terminal_codes("\x1b[31mred\x1b[0m \x1b]1337;File=inline=1:AAAA\x07after \x1b_Gf=100;AAAA\x1b\\end"), "red after end")

    def test_plain_text_is_unchanged(self) -> None:
        (message,) = self.process("warning: 250% of something\n").messages
        self.assertEqual(message.text, "warning: 250% of something")

    def test_runner_reads_a_bar_written_by_a_child_process(self) -> None:
        script = "import sys\nsys.stdout.write('\\n')\nfor n in (1, 2):\n    sys.stdout.write('\\x1b[1A\\x1b[KSampling... %d / 2 [█] %d%%\\n' % (n, n * 50))\nsys.stdout.write('\\x1b[1A\\x1b[KGenerated [█] 100%\\n')\nsys.stdout.flush()"
        result = DrawThingsProcessRunner(ProcessCommand((sys.executable, "-c", script)), output_processor=OutputProcessor(), handle_signals=False).run()
        self.assertTrue(result.succeeded)
        self.assertEqual([message.text for message in result.messages], ["Sampling... 1 / 2 [█] 50%", "Sampling... 2 / 2 [█] 100%", "Generated [█] 100%"])


if __name__ == "__main__":
    unittest.main()
