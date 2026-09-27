"""Tests for the strict YAML reader that every YAML file of the project goes through."""

from __future__ import annotations

from draw_things_control.core.global_config import load_global_config
from draw_things_control.core.yaml_files import is_yaml_file, parse_yaml_mapping, read_yaml_file
from draw_things_control.jobs.parsing import load_job
from tests.fixtures import JobTestCase, job_data


class StrictYamlTests(JobTestCase):
    def test_a_job_file_with_a_duplicate_key_is_refused_with_the_key_and_its_line(self) -> None:
        path = self.write_job(job_data())
        path.write_text(path.read_text(encoding="utf-8") + "name: another\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"(?s)Job file is not valid YAML: .*job\.yaml \(.*key 'name' appears twice.*line \d+"):
            load_job(path, self.global_config, self.params)

    def test_a_job_file_with_an_octal_looking_number_is_refused(self) -> None:
        path = self.write_job(job_data())
        path.write_text(path.read_text(encoding="utf-8").replace("run_count: 5", "run_count: 010"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "leading zero"):
            load_job(path, self.global_config, self.params)

    def test_the_global_configuration_is_read_strictly_too(self) -> None:
        path = self.root / "global.yaml"
        for text, problem in (
            (f"version: 1\ninput_directory: {self.input_directory}\noutput_directory: {self.output_directory}\nversion: 1\n", "key 'version' appears twice"),
            (f"version: 1\ninput_directory: {self.input_directory}\noutput_directory: {self.output_directory}\nhistory_retention_days: 014\n", "leading zero"),
        ):
            path.write_text(text, encoding="utf-8")
            with self.subTest(problem=problem), self.assertRaisesRegex(ValueError, f"(?s)Global configuration is not valid YAML: .*global\\.yaml \\(.*{problem}"):
                load_global_config(path)

    def test_messages_start_with_the_description_of_the_file(self) -> None:
        path = self.root / "x.yaml"
        for text, message in (("", "Thing is empty: "), ("- a\n", "Thing must contain one YAML mapping: ")):
            path.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, message):
                read_yaml_file(path, "Thing")
        with self.assertRaisesRegex(ValueError, "Thing not found: "):
            read_yaml_file(self.root / "absent.yaml", "Thing")

    def test_require_json_refuses_a_date_and_names_its_key(self) -> None:
        with self.assertRaisesRegex(ValueError, r"Thing .*: when is a date or time"):
            parse_yaml_mapping("when: 2026-09-27\n", self.root / "x.yaml", "Thing", require_json=True)
        self.assertEqual(list(parse_yaml_mapping("when: 2026-09-27\n", self.root / "x.yaml", "Thing")), ["when"])

    def test_yaml_suffixes_are_matched_in_any_case(self) -> None:
        self.assertEqual([is_yaml_file(name) for name in ("a.yaml", "a.YML", "a.json", "yaml")], [True, True, False, False])
