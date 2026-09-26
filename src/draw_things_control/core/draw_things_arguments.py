"""Typed argument objects for Draw Things and supervised test commands."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from draw_things_control.core.numbers import positive_whole, setting_number

# Every flag ``DrawThingsGenerateArguments.command`` writes, in order, with how: "value" (the flag and the attribute's
# text), "images" (``--image`` once per image, the primary first), "switch" (alone, when true), or "negatable" (the flag
# when true, ``--no-`` and the flag when false, nothing when None). The attribute is the flag's name in snake case.
GENERATE_FLAGS = (
    ("--models-dir", "value"), ("--model", "value"), ("--prompt", "value"), ("--prompt-file", "value"), ("--negative-prompt", "value"), ("--negative-prompt-file", "value"),
    ("--steps", "value"), ("--cfg", "value"), ("--width", "value"), ("--height", "value"), ("--frames", "value"), ("--strength", "value"), ("--seed", "value"), ("--config-json", "value"), ("--config-file", "value"),
    ("--image", "images"), ("--audio", "value"), ("--audio-encoder-file", "value"), ("--avc", "switch"), ("--segment-frames", "value"), ("--cond-frames", "value"), ("--output", "value"), ("--video-format", "value"),
    ("--terminal-image", "switch"), ("--terminal-image-protocol", "value"), ("--download-missing", "negatable"), ("--disable-preview", "switch"), ("--offline", "switch"),
    ("--remote", "switch"), ("--remote-url", "value"), ("--remote-port", "value"), ("--remote-tls", "negatable"), ("--remote-shared-secret", "value"),
    ("--cloud-compute", "switch"), ("--api-key", "value"), ("--cloud-api-base-url", "value"),
)  # fmt: skip
# The flags written with a value after them; the rest stand alone.
VALUE_FLAGS = frozenset(flag for flag, kind in GENERATE_FLAGS if kind in ("value", "images"))
# The flags whose value is a credential, which is never shown or saved.
SECRET_FLAGS = frozenset(("--api-key", "--remote-shared-secret"))


class CommandArguments(Protocol):
    """A typed object capable of producing an executable command."""

    @property
    def command(self) -> tuple[str, ...]:
        """Return the executable and its arguments."""


@dataclass(frozen=True)
class DrawThingsGenerateArguments:
    """Typed options for ``draw-things-cli generate``.

    The first image is the primary input; reference images retain their order.
    Optional values are omitted so the CLI can apply its recommended defaults.
    """

    model: str
    executable: str = "draw-things-cli"
    models_dir: Path | None = None
    prompt: str | None = None
    prompt_file: Path | str | None = None
    negative_prompt: str | None = None
    negative_prompt_file: Path | str | None = None
    steps: int | None = None
    cfg: float | None = None
    width: int | None = None
    height: int | None = None
    frames: int | None = None
    strength: float | None = None
    seed: int | None = None
    config_json: str | None = None
    config_file: Path | None = None
    image: Path | None = None
    reference_images: tuple[Path, ...] = ()
    audio: Path | None = None
    audio_encoder_file: str | None = None
    avc: bool = False
    segment_frames: int | None = None
    cond_frames: int | None = None
    output: Path | None = None
    video_format: str | None = None
    terminal_image: bool = False
    terminal_image_protocol: str | None = None
    download_missing: bool | None = None
    disable_preview: bool = False
    offline: bool = False
    remote: bool = False
    remote_url: str | None = None
    remote_port: int | None = None
    remote_tls: bool | None = None
    remote_shared_secret: str | None = None
    cloud_compute: bool = False
    api_key: str | None = None
    cloud_api_base_url: str | None = None

    def __post_init__(self) -> None:
        if not self.model or not self.executable:
            raise ValueError("model and executable must not be empty")
        if self.prompt is not None and self.prompt_file is not None:
            raise ValueError("--prompt and --prompt-file are mutually exclusive")
        if self.negative_prompt is not None and self.negative_prompt_file is not None:
            raise ValueError("--negative-prompt and --negative-prompt-file are mutually exclusive")
        if self.prompt_file is not None and self.negative_prompt_file is not None and str(self.prompt_file) == str(self.negative_prompt_file) == "-":
            raise ValueError("Only one prompt file can read from stdin")
        if self.reference_images and self.image is None:
            raise ValueError("Reference images require a primary image")
        if self.strength is not None and not 0 <= self.strength <= 1:
            raise ValueError("--strength must be between 0 and 1")
        for name in ("width", "height"):
            value = getattr(self, name)
            if value is not None and (value <= 0 or value % 64):
                raise ValueError(f"--{name} must be a positive multiple of 64")
        for name in ("steps", "frames"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"--{name} must be positive")
        if self.avc and (self.image is None or self.reference_images or self.audio is None):
            raise ValueError("--avc requires exactly one --image and an --audio file")
        if not self.avc and (self.segment_frames is not None or self.cond_frames is not None):
            raise ValueError("--segment-frames and --cond-frames require --avc")
        if self.video_format is not None:
            if self.video_format not in {"prores4444", "prores422hq", "h264", "hevc"}:
                raise ValueError("Unsupported --video-format")
            if self.output is None or self.output.suffix.lower() not in {".mov", ".mp4"}:
                raise ValueError("--video-format requires a .mov or .mp4 output")
            if self.video_format.startswith("prores") and self.output.suffix.lower() != ".mov":
                raise ValueError("ProRes requires a .mov output")
        if self.output is not None and self.output.suffix.lower() not in {".png", ".mov", ".mp4"}:
            raise ValueError("--output must end in .png, .mov, or .mp4")
        if self.terminal_image and self.output is not None and self.output.suffix.lower() != ".png":
            raise ValueError("--terminal-image requires PNG output")
        if self.terminal_image_protocol not in (None, "auto", "iterm2", "kitty"):
            raise ValueError("Unsupported --terminal-image-protocol")
        if self.remote and self.cloud_compute:
            raise ValueError("--remote and --cloud-compute are mutually exclusive")
        if not self.remote and any(value is not None for value in (self.remote_url, self.remote_port, self.remote_tls, self.remote_shared_secret)):
            raise ValueError("Remote connection options require --remote")
        if not self.cloud_compute and (self.api_key is not None or self.cloud_api_base_url is not None):
            raise ValueError("Cloud connection options require --cloud-compute")

    @property
    def command(self) -> tuple[str, ...]:
        """Build the command as an argv tuple without shell interpolation."""
        command = [self.executable, "generate"]
        for flag, kind in GENERATE_FLAGS:
            value = getattr(self, flag[2:].replace("-", "_"))
            if kind == "images":
                for image in (() if value is None else (value,)) + self.reference_images:
                    command.extend((flag, str(image)))
            elif kind == "value":
                if value is not None:
                    command.extend((flag, str(value)))
            elif kind == "switch":
                if value:
                    command.append(flag)
            elif value is not None:
                command.append(flag if value else "--no-" + flag[2:])
        return tuple(command)


@dataclass(frozen=True)
class CommandSettings:
    """Generation settings read back from a saved ``draw-things-cli generate`` command; each None when it cannot be told."""

    model: str | None = None
    refiner_model: str | None = None
    # The share of the steps after which the refiner takes over, 0 to 1.
    refiner_start: float | None = None
    cfg: float | None = None
    shift: float | None = None
    steps: int | None = None


def command_settings(command: Sequence[str]) -> CommandSettings:
    """The settings a command ran with: a flag wins over the same key in ``--config-json``, as draw-things-cli applies them.

    A setting in neither is None: the model's recommended value is not in the command. Never raises; an unreadable
    ``--config-json``, a value of the wrong type, or a flag with no value leaves only the settings it affects as None.
    """
    flags: dict[str, str] = {}
    for flag, value in command_arguments(command):
        if value is not None:
            flags.setdefault(flag, value)
    config = _config_json(flags.get("--config-json"))
    return CommandSettings(
        model=flags.get("--model") or _text(config.get("model")),
        refiner_model=_text(config.get("refinerModel")),
        refiner_start=setting_number(config.get("refinerStart")),
        cfg=setting_number(flags["--cfg"]) if "--cfg" in flags else setting_number(config.get("guidanceScale")),
        shift=setting_number(config.get("shift")),
        steps=positive_whole(flags["--steps"]) if "--steps" in flags else positive_whole(config.get("steps")),
    )


def command_arguments(command: Sequence[str]) -> list[tuple[str, str | None]]:
    """Each flag of a saved ``draw-things-cli generate`` command, in order, with its value, or None for a flag that stands alone.

    The executable and the subcommand are skipped. A flag's value is taken as it is, so a prompt that reads like a flag is
    never parsed as one; a value flag at the very end has no value (None). Words that are not flags are skipped.
    """
    arguments: list[tuple[str, str | None]] = []
    index = 1
    while index < len(command):
        token = str(command[index])
        if token in VALUE_FLAGS:
            arguments.append((token, str(command[index + 1]) if index + 1 < len(command) else None))
            index += 2
        else:
            if token.startswith("--"):
                arguments.append((token, None))
            index += 1
    return arguments


def config_json(command: Sequence[str]) -> dict[str, Any]:
    """The command's ``--config-json`` as a mapping; empty when it has none or it cannot be read."""
    return _config_json(next((value for flag, value in command_arguments(command) if flag == "--config-json"), None))


def _config_json(text: str | None) -> dict[str, Any]:
    if text is None:
        return {}
    try:
        config = json.loads(text)
    except ValueError:
        return {}
    return config if isinstance(config, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
