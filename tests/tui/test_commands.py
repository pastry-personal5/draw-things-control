"""Tests for the command line's parsing and completion."""

from __future__ import annotations

import asyncio
import unittest

from draw_things_control.tui.commands import COMMAND_NAMES, Command, CommandError, CommandSuggester, completions, help_text, parse, usage
from draw_things_control.tui.history import MAX_ID, parse_id


class ParseTests(unittest.TestCase):
    def test_a_line_splits_as_a_shell_would(self) -> None:
        self.assertEqual(parse("/apply walk.yaml"), Command("apply", ("walk.yaml",)))
        self.assertEqual(parse("  /describe job '[b] walk.yaml'  "), Command("describe", ("job", "[b] walk.yaml")))
        self.assertEqual(parse("/GET Positive 12 3"), Command("get", ("Positive", "12", "3")))
        self.assertEqual(parse('/filter name "sunset walk"'), Command("filter", ("name", "sunset walk")))
        self.assertEqual(parse("/APPLY walk"), Command("apply", ("walk",)))
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
            parse("/apply 'walk")

    def test_usage_and_help_list_every_form(self) -> None:
        self.assertEqual(usage("reveal"), "/reveal <Execution ID> [RUN]")
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
        self.assertEqual(completions("/h", self.JOBS), ["/help"])
        self.assertEqual(completions("/", self.JOBS), [f"/{name}" for name in COMMAND_NAMES])
        self.assertEqual(completions("", self.JOBS), [f"/{name}" for name in COMMAND_NAMES])
        self.assertEqual(completions("/help", self.JOBS), [])
        self.assertEqual(completions("h", self.JOBS), [])

    def test_job_names_complete_after_run_and_job(self) -> None:
        self.assertEqual(completions("/apply wa", self.JOBS), ["/apply walk.yaml", "/apply wave.yml"])
        self.assertEqual(completions("/describe job wav", self.JOBS), ["/describe job wave.yml"])
        # A name with spaces is completed quoted, so it parses as one argument.
        self.assertEqual(completions("/apply '[", self.JOBS), ["/apply '[b] walk.yaml'"])
        self.assertEqual(completions("/stop w", self.JOBS), [])

    def test_names_that_need_quoting_complete_as_typed_and_parse_back(self) -> None:
        jobs = ["my job.yaml", "it's.yaml", "[b] walk.yaml"]
        cases = {
            # Unquoted: special characters are escaped, so the suggestion still begins with the typed text.
            "/apply my": ["/apply my\\ job.yaml"],
            "/apply my\\ j": ["/apply my\\ job.yaml"],
            "/describe job it": ["/describe job it\\'s.yaml"],
            # Inside a quote the user opened, the name is closed with it; a name holding that quote is not offered.
            '/apply "my': ['/apply "my job.yaml"'],
            "/apply 'it": [],
            "/apply '[": ["/apply '[b] walk.yaml'"],
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                found = completions(line, jobs)
                self.assertEqual(found, expected)
                for completed in found:
                    self.assertTrue(completed.startswith(line))
                    # The name parses back whole, as the last argument.
                    self.assertIn(parse(completed).arguments[-1], jobs)

    def test_the_command_word_completes_in_any_case(self) -> None:
        self.assertEqual(completions("/APPLY wa", self.JOBS), ["/APPLY walk.yaml", "/APPLY wave.yml"])
        self.assertEqual(completions("/Filter status s", self.JOBS), ["/Filter status succeeded"])

    def test_the_get_and_describe_words_complete(self) -> None:
        self.assertEqual(completions("/get ", self.JOBS), ["/get jobs", "/get history", "/get prompts", "/get positive", "/get negative", "/get param", "/get parameters"])
        self.assertEqual(completions("/get par", self.JOBS), ["/get param", "/get parameters"])
        self.assertEqual(completions("/describe ", self.JOBS), ["/describe job", "/describe execution"])
        self.assertEqual(completions("/DESCRIBE Job w", self.JOBS), ["/DESCRIBE Job walk.yaml", "/DESCRIBE Job wave.yml"])
        self.assertEqual(completions("/get positive 1", self.JOBS), [])

    def test_the_replaced_commands_name_their_new_form(self) -> None:
        for line, new in (("/jobs", "/get jobs"), ("/job walk", "/describe job <Job ID>"), ("/History", "/get history"), ("/execution E0012", "/describe execution <Execution ID>"), ("/run walk", "/apply <Job ID>")):
            with self.subTest(line=line), self.assertRaisesRegex(CommandError, f"^{line.split()[0]} is now {new}; type /help$"):
                parse(line)
        self.assertNotIn("jobs", COMMAND_NAMES)
        self.assertEqual(usage("get", "positive"), "/get positive <Execution ID> [RUN]")
        self.assertEqual(usage("describe"), "/describe job <Job ID> | /describe execution <Execution ID>")
        self.assertEqual(usage("describe", "execution"), "/describe execution <Execution ID>")

    def test_ids_and_sort_words_complete(self) -> None:
        # Job IDs complete a JOB before the file names; execution IDs complete an ID, keeping what was typed.
        self.assertEqual(completions("/apply J", self.JOBS, ["J0001", "J0002"]), ["/apply J0001", "/apply J0002"])
        self.assertEqual(completions("/describe job J0002", self.JOBS, ["J0001", "J0002"]), [])
        executions = ["E0012", "E0011", "E0003"]
        self.assertEqual(completions("/describe execution E001", self.JOBS, (), executions), ["/describe execution E0012", "/describe execution E0011"])
        self.assertEqual(completions("/describe j0", self.JOBS, ["J0001", "J0002"], executions), ["/describe j0001", "/describe j0002"])
        self.assertEqual(completions("/describe e001", self.JOBS, ["J0001"], executions), ["/describe e0012", "/describe e0011"])
        self.assertEqual(completions("/describe j", self.JOBS, ["J0001"], executions), ["/describe job", "/describe j0001"])
        self.assertEqual(completions("/reveal e000", self.JOBS, (), executions), ["/reveal e0003"])
        self.assertEqual(completions("/get param E00", self.JOBS, (), executions), ["/get param E0012", "/get param E0011", "/get param E0003"])
        self.assertEqual(completions("/get jobs E", self.JOBS, (), executions), [])
        self.assertEqual(completions("/sort ", self.JOBS), ["/sort jobs"])
        self.assertEqual(completions("/sort jobs c", self.JOBS), ["/sort jobs changed"])
        self.assertEqual(completions("/sort jobs name d", self.JOBS), ["/sort jobs name desc"])

    def test_filter_words_and_statuses_complete(self) -> None:
        self.assertEqual(completions("/filter ", self.JOBS), ["/filter status", "/filter name", "/filter off"])
        self.assertEqual(completions("/filter o", self.JOBS), ["/filter off"])
        self.assertEqual(completions("/filter status f", self.JOBS), ["/filter status failed"])
        self.assertEqual(completions("/filter name x", self.JOBS), [])

    def test_the_suggester_reads_the_job_names_on_each_call(self) -> None:
        names: list[str] = []
        suggester = CommandSuggester(lambda: names)
        self.assertIsNone(asyncio.run(suggester.get_suggestion("/apply w")))
        names.append("walk.yaml")
        self.assertEqual(asyncio.run(suggester.get_suggestion("/apply w")), "/apply walk.yaml")
