"""Typed argument objects for Draw Things and supervised test commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


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

        def add_value(flag: str, value: object | None) -> None:
            if value is not None:
                command.extend((flag, str(value)))

        add_value("--models-dir", self.models_dir)
        add_value("--model", self.model)
        add_value("--prompt", self.prompt)
        add_value("--prompt-file", self.prompt_file)
        add_value("--negative-prompt", self.negative_prompt)
        add_value("--negative-prompt-file", self.negative_prompt_file)
        for flag, value in (
            ("--steps", self.steps),
            ("--cfg", self.cfg),
            ("--width", self.width),
            ("--height", self.height),
            ("--frames", self.frames),
            ("--strength", self.strength),
            ("--seed", self.seed),
            ("--config-json", self.config_json),
            ("--config-file", self.config_file),
        ):
            add_value(flag, value)
        for image in (() if self.image is None else (self.image,)) + self.reference_images:
            add_value("--image", image)
        add_value("--audio", self.audio)
        add_value("--audio-encoder-file", self.audio_encoder_file)
        if self.avc:
            command.append("--avc")
        add_value("--segment-frames", self.segment_frames)
        add_value("--cond-frames", self.cond_frames)
        add_value("--output", self.output)
        add_value("--video-format", self.video_format)
        if self.terminal_image:
            command.append("--terminal-image")
        add_value("--terminal-image-protocol", self.terminal_image_protocol)
        if self.download_missing is not None:
            command.append("--download-missing" if self.download_missing else "--no-download-missing")
        if self.disable_preview:
            command.append("--disable-preview")
        if self.offline:
            command.append("--offline")
        if self.remote:
            command.append("--remote")
        add_value("--remote-url", self.remote_url)
        add_value("--remote-port", self.remote_port)
        if self.remote_tls is not None:
            command.append("--remote-tls" if self.remote_tls else "--no-remote-tls")
        add_value("--remote-shared-secret", self.remote_shared_secret)
        if self.cloud_compute:
            command.append("--cloud-compute")
        add_value("--api-key", self.api_key)
        add_value("--cloud-api-base-url", self.cloud_api_base_url)
        return tuple(command)
