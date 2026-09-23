"""Tests for typed Draw Things command arguments."""

import unittest
from pathlib import Path

from draw_things_arguments import DrawThingsGenerateArguments


class GenerateArgumentsTests(unittest.TestCase):
    def test_avc_requires_one_image_and_audio(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one --image"):
            DrawThingsGenerateArguments(model="example.ckpt", avc=True)

    def test_prompt_text_and_file_are_exclusive(self) -> None:
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            DrawThingsGenerateArguments(model="example.ckpt", prompt="text", prompt_file=Path("prompt.txt"))

    def test_remote_flags_are_kept_in_command(self) -> None:
        arguments = DrawThingsGenerateArguments(model="example.ckpt", remote=True, remote_url="127.0.0.1", remote_tls=False)
        self.assertIn("--remote", arguments.command)
        self.assertIn("--no-remote-tls", arguments.command)
