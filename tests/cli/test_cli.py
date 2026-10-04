"""Tests for the public command-line interface."""

import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from draw_things_control.cli.app import app, load_config


class DrawThingsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_load_config_rejects_non_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("[]\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must contain one YAML mapping"):
                load_config(path)

    def test_json_configurations_are_refused_and_left_as_they_are(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "wan.json").write_text('{"model": "example.ckpt"}', encoding="utf-8")
            (root / "solo.json").write_text("{}", encoding="utf-8")
            (root / "wan.yaml").write_text("model: example.ckpt\n", encoding="utf-8")
            self.assertEqual(self.runner.invoke(app, ["validate-config", str(root / "wan.json")]).exit_code, 2)
            self.assertEqual(self.runner.invoke(app, ["validate-config", str(root / "wan.json")]).exit_code, 2)
            with self.assertRaisesRegex(ValueError, r"wan\.json is JSON; use wan\.yaml instead"):
                load_config(root / "wan.json")
            with self.assertRaisesRegex(ValueError, r"solo\.json is JSON, but this command needs a YAML configuration; write solo\.yaml in"):
                load_config(root / "solo.json")
            with self.assertRaisesRegex(ValueError, r"must be a YAML file"):
                load_config(root / "wan.txt")
            self.assertEqual((root / "wan.json").read_text(encoding="utf-8"), '{"model": "example.ckpt"}')
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["solo.json", "wan.json", "wan.yaml"])

    def test_generate_requires_a_timeout(self) -> None:
        result = self.runner.invoke(app, ["generate", "--model", "example.ckpt", "--output", "cube.png"])
        self.assertEqual(result.exit_code, 2)

    def test_generate_rejects_removed_remote_and_credential_options(self) -> None:
        for option in ("--remote", "--cloud-compute", "--api-key", "--config-json", "--models-dir", "--terminal-image"):
            with self.subTest(option=option):
                result = self.runner.invoke(app, ["generate", "--timeout", "60", "--model", "example.ckpt", "--output", "cube.png", option])
                self.assertEqual(result.exit_code, 2, result.output)

    def test_conflicting_prompts_return_input_error(self) -> None:
        result = self.runner.invoke(app, ["generate", "--timeout", "60", "--model", "example.ckpt", "--output", "cube.png", "--prompt", "one", "--prompt-file", "-", "--dry-run"])
        self.assertEqual(result.exit_code, 2)

    def test_validate_config_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("model: example.ckpt\n", encoding="utf-8")
            result = self.runner.invoke(app, ["validate-config", str(config)])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("example.ckpt", result.stdout)

    def test_validate_config_accepts_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("# a comment\nmodel: example.ckpt\n", encoding="utf-8")
            result = self.runner.invoke(app, ["validate-config", str(config)])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(result.stdout, f"Valid configuration: {config} (model: example.ckpt)\n")

    def test_validate_config_rejects_invalid_yaml_with_exit_code_2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for name, text in (("date.yaml", "model: m\nseed: 2026-09-25\n"), ("nan.yml", "shift: .nan\n"), ("list.yaml", "- m\n"), ("keys.yaml", "1: m\n")):
                with self.subTest(name):
                    config = Path(directory) / name
                    config.write_text(text, encoding="utf-8")
                    result = self.runner.invoke(app, ["validate-config", str(config)])
                    self.assertEqual(result.exit_code, 2, result.output)
