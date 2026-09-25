"""Tests for the command line's parsing and completion."""

from __future__ import annotations

import asyncio
import unittest

from draw_things_control.tui.commands import COMMAND_NAMES, Command, CommandError, CommandSuggester, completions, help_text, parse, usage
from draw_things_control.tui.history import MAX_ID, parse_id


class ParseTests(unittest.TestCase):
    def test_a_line_splits_as_a_shell_would(self) -> None:
        self.assertEqual(parse("/run walk.yaml"), Command("run", ("walk.yaml",)))
        self.assertEqual(parse("  /job '[b] walk.yaml'  "), Command("job", ("[b] walk.yaml",)))
        self.assertEqual(parse('/filter name "sunset walk"'), Command("filter", ("name", "sunset walk")))
        self.assertEqual(parse("/RUN walk"), Command("run", ("walk",)))
        self.assertIsNone(parse("   "))

    def test_a_line_without_the_slash_is_refused(self) -> None:
        for line in ("run walk", "help", "quit", "?", "exit"):
            with self.subTest(line=line), self.assertRaisesRegex(CommandError, "^Commands begin with /; type /help$"):
                parse(line)

    def test_an_unknown_command_an_old_alias_and_bad_quoting_are_refused(self) -> None:
        for line, word in (("/launch walk", "/launch"), ("/exit", "/exit"), ("/show walk", "/show"), ("/", "/")):
            with self.subTest(line=line), self.assertRaisesRegex(CommandError, f"^Unknown command '{word}'; type /help$"):
                parse(line)
        with self.assertRaisesRegex(CommandError, "Cannot read the command: No closing quotation"):
            parse("/run 'walk")

    def test_usage_and_help_list_every_form(self) -> None:
        self.assertEqual(usage("reveal"), "/reveal ID [RUN]")
        self.assertEqual(usage("filter"), "/filter status STATUS | /filter name TEXT | /filter off")
        text = str(help_text())
        for name in COMMAND_NAMES:
            self.assertIn(f"  /{name}", text)
        self.assertIn("Ctrl-C", text)
        self.assertIn("Up/Down", text)


class IdTests(unittest.TestCase):
    def test_only_ascii_digits_that_sqlite_can_hold_are_ids(self) -> None:
        self.assertEqual(parse_id("42"), 42)
        self.assertEqual(parse_id(str(MAX_ID)), MAX_ID)
        for text in ("0", "-1", "+1", "1.0", "²", "١", "٣", "99999999999999999999", str(MAX_ID + 1), "", " 1"):
            with self.subTest(text=text):
                self.assertIsNone(parse_id(text))


class CompletionTests(unittest.TestCase):
    JOBS = ["walk.yaml", "wave.yml", "[b] walk.yaml"]

    def test_command_names_complete_after_the_slash(self) -> None:
        self.assertEqual(completions("/h", self.JOBS), ["/help", "/history"])
        self.assertEqual(completions("/", self.JOBS), [f"/{name}" for name in COMMAND_NAMES])
        self.assertEqual(completions("", self.JOBS), [f"/{name}" for name in COMMAND_NAMES])
        self.assertEqual(completions("/help", self.JOBS), [])
        self.assertEqual(completions("h", self.JOBS), [])

    def test_job_names_complete_after_run_and_job(self) -> None:
        self.assertEqual(completions("/run wa", self.JOBS), ["/run walk.yaml", "/run wave.yml"])
        self.assertEqual(completions("/job wav", self.JOBS), ["/job wave.yml"])
        # A name with spaces is completed quoted, so it parses as one argument.
        self.assertEqual(completions("/run '[", self.JOBS), ["/run '[b] walk.yaml'"])
        self.assertEqual(completions("/stop w", self.JOBS), [])

    def test_names_that_need_quoting_complete_as_typed_and_parse_back(self) -> None:
        jobs = ["my job.yaml", "it's.yaml", "[b] walk.yaml"]
        cases = {
            # Unquoted: special characters are escaped, so the suggestion still begins with the typed text.
            "/run my": ["/run my\\ job.yaml"],
            "/run my\\ j": ["/run my\\ job.yaml"],
            "/job it": ["/job it\\'s.yaml"],
            # Inside a quote the user opened, the name is closed with it; a name holding that quote is not offered.
            '/run "my': ['/run "my job.yaml"'],
            "/run 'it": [],
            "/run '[": ["/run '[b] walk.yaml'"],
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                found = completions(line, jobs)
                self.assertEqual(found, expected)
                for completed in found:
                    self.assertTrue(completed.startswith(line))
                    self.assertEqual(len(parse(completed).arguments), 1)

    def test_the_command_word_completes_in_any_case(self) -> None:
        self.assertEqual(completions("/RUN wa", self.JOBS), ["/RUN walk.yaml", "/RUN wave.yml"])
        self.assertEqual(completions("/Filter status s", self.JOBS), ["/Filter status succeeded"])

    def test_filter_words_and_statuses_complete(self) -> None:
        self.assertEqual(completions("/filter ", self.JOBS), ["/filter status", "/filter name", "/filter off"])
        self.assertEqual(completions("/filter o", self.JOBS), ["/filter off"])
        self.assertEqual(completions("/filter status f", self.JOBS), ["/filter status failed"])
        self.assertEqual(completions("/filter name x", self.JOBS), [])

    def test_the_suggester_reads_the_job_names_on_each_call(self) -> None:
        names: list[str] = []
        suggester = CommandSuggester(lambda: names)
        self.assertIsNone(asyncio.run(suggester.get_suggestion("/run w")))
        names.append("walk.yaml")
        self.assertEqual(asyncio.run(suggester.get_suggestion("/run w")), "/run walk.yaml")
