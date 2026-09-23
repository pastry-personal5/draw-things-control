"""Tests for the public command-line interface."""

import json
import shlex
import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from main import app, load_config


class DrawThingsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_load_config_rejects_non_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "JSON object"):
                load_config(path)

    def test_example_shape_uses_model_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            image = root / "source.png"
            config.write_text(json.dumps({"model": "example.ckpt"}), encoding="utf-8")
            image.touch()
            result = self.runner.invoke(app, ["generate", "--config-file", str(config), "--image", str(image), "--output", str(root / "output.mov"), "--dry-run"])
            self.assertEqual(result.exit_code, 0, result.output)
            command = shlex.split(result.stdout.strip())
            self.assertIn("--config-file", command)
            self.assertEqual(command[command.index("--model") + 1], "example.ckpt")
            self.assertEqual(command[command.index("--image") + 1], str(image.resolve()))

    def test_explicit_model_overrides_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(json.dumps({"model": "config.ckpt"}), encoding="utf-8")
            result = self.runner.invoke(app, ["generate", "--config", str(config), "-m", "override.ckpt", "--dry-run"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("--model override.ckpt", result.stdout)

    def test_repeated_images_and_numeric_zero_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = (root / "first.png", root / "second.png")
            for image in images:
                image.touch()
            result = self.runner.invoke(app, ["generate", "--model", "example.ckpt", "--image", str(images[0]), "--image", str(images[1]), "--cfg", "0", "--seed", "0", "--strength", "0", "--output", str(root / "clip.mov"), "--video-format", "h264", "--no-download-missing", "--dry-run"])
            self.assertEqual(result.exit_code, 0, result.output)
            command = shlex.split(result.stdout.strip())
            self.assertEqual([command[index + 1] for index, token in enumerate(command) if token == "--image"], [str(image.resolve()) for image in images])
            self.assertEqual(command[command.index("--cfg") + 1], "0.0")
            self.assertEqual(command[command.index("--seed") + 1], "0")
            self.assertIn("--no-download-missing", command)

    def test_text_only_generation_has_no_image_or_output(self) -> None:
        result = self.runner.invoke(app, ["generate", "--model", "example.ckpt", "--prompt", "a red cube", "--dry-run"])
        self.assertEqual(result.exit_code, 0, result.output)
        command = shlex.split(result.stdout.strip())
        self.assertNotIn("--image", command)
        self.assertNotIn("--output", command)

    def test_cloud_credentials_are_redacted_in_preview(self) -> None:
        result = self.runner.invoke(app, ["generate", "--model", "example.ckpt", "--cloud-compute", "--api-key", "private-key", "--dry-run"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("[redacted]", result.stdout)
        self.assertNotIn("private-key", result.stdout)

    def test_remote_tls_false_is_forwarded(self) -> None:
        result = self.runner.invoke(app, ["generate", "--model", "example.ckpt", "--remote", "--remote-url", "127.0.0.1", "--no-remote-tls", "--dry-run"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("--no-remote-tls", shlex.split(result.stdout.strip()))

    def test_conflicting_prompts_return_input_error(self) -> None:
        result = self.runner.invoke(app, ["generate", "--model", "example.ckpt", "--prompt", "one", "--prompt-file", "-", "--dry-run"])
        self.assertEqual(result.exit_code, 2)

    def test_validate_config_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text('{"model": "example.ckpt"}', encoding="utf-8")
            result = self.runner.invoke(app, ["validate-config", str(config)])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("example.ckpt", result.stdout)
