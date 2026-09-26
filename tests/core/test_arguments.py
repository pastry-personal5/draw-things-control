"""Tests for typed Draw Things command arguments."""

import json
import unittest
from pathlib import Path

from draw_things_control.core.draw_things_arguments import CommandSettings, DrawThingsGenerateArguments, command_arguments, command_settings
from draw_things_control.core.numbers import positive_whole, setting_number


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

    def test_every_flag_is_written_in_order_and_parses_back(self) -> None:
        remote = DrawThingsGenerateArguments(
            model="m.ckpt", models_dir=Path("/models"), prompt="--cfg 9", negative_prompt="blur", steps=8, cfg=2.5, width=832, height=448, frames=81, strength=0.5, seed=7, config_json="{}", config_file=Path("c.json"),
            image=Path("a.png"), audio=Path("s.wav"), audio_encoder_file="enc", avc=True, segment_frames=4, cond_frames=2, output=Path("o.mov"), video_format="h264",
            terminal_image_protocol="kitty", download_missing=False, disable_preview=True, offline=True, remote=True, remote_url="host", remote_port=7859, remote_tls=True, remote_shared_secret="s",
        )  # fmt: skip
        self.assertEqual(
            remote.command[2:],
            (
                "--models-dir", "/models", "--model", "m.ckpt", "--prompt", "--cfg 9", "--negative-prompt", "blur", "--steps", "8", "--cfg", "2.5", "--width", "832", "--height", "448", "--frames", "81", "--strength", "0.5", "--seed", "7",
                "--config-json", "{}", "--config-file", "c.json", "--image", "a.png", "--audio", "s.wav", "--audio-encoder-file", "enc", "--avc", "--segment-frames", "4", "--cond-frames", "2", "--output", "o.mov",
                "--video-format", "h264", "--terminal-image-protocol", "kitty", "--no-download-missing", "--disable-preview", "--offline", "--remote", "--remote-url", "host", "--remote-port", "7859", "--remote-tls", "--remote-shared-secret", "s",
            ),
        )  # fmt: skip
        cloud = DrawThingsGenerateArguments(model="m.ckpt", prompt_file=Path("p.txt"), negative_prompt_file=Path("n.txt"), image=Path("a.png"), reference_images=(Path("b.png"), Path("c.png")), output=Path("o.png"), terminal_image=True, download_missing=True, cloud_compute=True, api_key="k", cloud_api_base_url="https://api")
        self.assertEqual(cloud.command[2:], ("--model", "m.ckpt", "--prompt-file", "p.txt", "--negative-prompt-file", "n.txt", "--image", "a.png", "--image", "b.png", "--image", "c.png", "--output", "o.png", "--terminal-image", "--download-missing", "--cloud-compute", "--api-key", "k", "--cloud-api-base-url", "https://api"))
        for arguments in (remote, cloud):
            # Every value is read back as its flag's value, so the parser knows each flag the builder writes.
            parsed = [token for flag, value in command_arguments(arguments.command) for token in (flag, value) if token is not None]
            self.assertEqual(tuple(parsed), arguments.command[2:])


WAN_CONFIG = {"model": "wan_hne.ckpt", "refinerModel": "wan_lne.ckpt", "refinerStart": 0.1, "guidanceScale": 5, "shift": 3.99, "steps": 40, "numFrames": 81, "width": 832, "height": 448}


def saved(*flags: str, config: object = WAN_CONFIG) -> list[str]:
    """A command as the state store keeps it."""
    command = ["draw-things-cli", "generate", "--model", "wan_hne.ckpt", "--prompt", "a walk", *flags]
    if config is not None:
        command += ["--config-json", config if isinstance(config, str) else json.dumps(config)]
    return [*command, "--output", "/out/walk.mov"]


class CommandSettingsTests(unittest.TestCase):
    def test_the_settings_are_read_from_the_config_json(self) -> None:
        self.assertEqual(command_settings(saved()), CommandSettings(model="wan_hne.ckpt", refiner_model="wan_lne.ckpt", refiner_start=0.1, cfg=5.0, shift=3.99, steps=40))

    def test_a_flag_wins_over_the_same_key_in_the_config_json(self) -> None:
        settings = command_settings(saved("--steps", "8", "--cfg", "2.5"))
        self.assertEqual((settings.steps, settings.cfg), (8, 2.5))

    def test_a_command_the_arguments_build_reads_back(self) -> None:
        arguments = DrawThingsGenerateArguments(model="flux.ckpt", prompt="--cfg 9", steps=4, config_json=json.dumps({"guidanceScale": 3.5, "shift": 1}), output=Path("/out/a.png"))
        # The prompt reads like a flag, but it is --prompt's value and is never parsed as one.
        self.assertEqual(command_settings(arguments.command), CommandSettings(model="flux.ckpt", cfg=3.5, shift=1.0, steps=4))

    def test_no_refiner_and_missing_settings_are_none(self) -> None:
        settings = command_settings(saved(config={"steps": 20}))
        self.assertEqual(settings, CommandSettings(model="wan_hne.ckpt", steps=20))
        self.assertIsNone(command_settings(saved(config={"refinerModel": ""})).refiner_model)

    def test_an_unreadable_command_never_raises(self) -> None:
        cases = {
            "not json": saved(config="{not json"),
            "not a mapping": saved(config="[1, 2]"),
            "no config": saved(config=None),
            "wrong types": saved(config={"guidanceScale": True, "shift": "high", "steps": 2.5, "refinerModel": 7, "refinerStart": None}),
            "not finite": saved(config='{"shift": NaN, "guidanceScale": Infinity}'),
            "too large": saved(config='{"steps": 1' + "0" * 400 + ', "shift": -1' + "0" * 400 + "}"),
        }
        for name, command in cases.items():
            with self.subTest(name):
                self.assertEqual(command_settings(command), CommandSettings(model="wan_hne.ckpt"))
        self.assertEqual(command_settings(["draw-things-cli", "generate", "--steps"]), CommandSettings())
        self.assertEqual(command_settings(["draw-things-cli", "generate", "--cfg", "much"]), CommandSettings())
        self.assertEqual(command_settings([]), CommandSettings())

    def test_the_model_falls_back_to_the_config_json(self) -> None:
        command = ["draw-things-cli", "generate", "--config-json", json.dumps({"model": "sd.ckpt"})]
        self.assertEqual(command_settings(command).model, "sd.ckpt")


class NumberTests(unittest.TestCase):
    def test_numbers_from_json_text_and_ffprobe_read_alike(self) -> None:
        self.assertEqual([setting_number(value) for value in (5, 5.0, "5", "5.0", True, "N/A", None, 10**400, float("nan"))], [5.0, 5.0, 5.0, 5.0, None, None, None, None, None])
        self.assertEqual([positive_whole(value) for value in (81, 81.0, "81", "81.5", 0, -1, "0", False, "N/A", 10**400)], [81, 81, 81, None, None, None, None, None, None, None])
