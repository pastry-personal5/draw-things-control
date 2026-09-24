"""Prepare and execute generation requests independently of the CLI."""

from __future__ import annotations

import json
import shlex
import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from draw_things_control.core.draw_things_arguments import DrawThingsGenerateArguments


class RunResult(Protocol):
    """The process outcome needed by the generation use case."""

    return_code: int
    timed_out: bool
    termination_signal: signal.Signals | None


class Runner(Protocol):
    """A runner that executes one prepared generation request."""

    def run(self) -> RunResult: ...


RunnerFactory = Callable[[DrawThingsGenerateArguments, float | None, float], Runner]


@dataclass(frozen=True)
class GenerationOutcome:
    """User-facing outcome without subprocess implementation details."""

    exit_code: int
    command_preview: str | None = None
    timed_out: bool = False
    termination_signal: signal.Signals | None = None


class GenerationService:
    """Resolve inputs, create Draw Things arguments, and run one request."""

    def __init__(
        self,
        runner_factory: RunnerFactory,
        find_executable: Callable[[str], str | None],
        config_loader: Callable[[Path], dict[str, Any]],
    ) -> None:
        self._runner_factory = runner_factory
        self._find_executable = find_executable
        self._config_loader = config_loader

    def prepare(self, options: Mapping[str, Any]) -> DrawThingsGenerateArguments:
        """Validate files and config, then construct typed CLI arguments."""
        config_file = options["config_file"]
        config = config_file.expanduser().resolve() if config_file is not None else None
        settings = dict(self._config_loader(config)) if config is not None else {}
        config_json = options["config_json"]
        if config_json is not None:
            try:
                inline_settings = json.loads(config_json)
            except json.JSONDecodeError as error:
                raise ValueError(f"--config-json is not valid JSON: {error.msg}") from error
            if not isinstance(inline_settings, dict):
                raise ValueError("--config-json must contain a JSON object")
            settings.update(inline_settings)
        model = options["model"] or settings.get("model")
        if not model:
            raise ValueError("A model must be set in the configuration or passed with --model")

        images = tuple(self._input_file(image, "Input image") for image in options["image"] or ())
        audio = self._input_file(options["audio"], "Audio file") if options["audio"] is not None else None
        output = options["output"].expanduser() if options["output"] is not None else None
        if output is not None and not output.parent.is_dir():
            raise ValueError(f"Output directory does not exist: {output.parent}")

        return DrawThingsGenerateArguments(
            model=str(model),
            executable=options["executable"],
            models_dir=options["models_dir"].expanduser() if options["models_dir"] is not None else None,
            prompt=options["prompt"],
            prompt_file=self._prompt_file(options["prompt_file"], "Prompt file"),
            negative_prompt=options["negative_prompt"],
            negative_prompt_file=self._prompt_file(options["negative_prompt_file"], "Negative prompt file"),
            steps=options["steps"],
            cfg=options["cfg"],
            width=options["width"],
            height=options["height"],
            frames=options["frames"],
            strength=options["strength"],
            seed=options["seed"],
            config_json=config_json,
            config_file=config,
            image=images[0] if images else None,
            reference_images=images[1:],
            audio=audio,
            audio_encoder_file=options["audio_encoder_file"],
            avc=options["avc"],
            segment_frames=options["segment_frames"],
            cond_frames=options["cond_frames"],
            output=output,
            video_format=options["video_format"],
            terminal_image=options["terminal_image"],
            terminal_image_protocol=options["terminal_image_protocol"],
            download_missing=options["download_missing"],
            disable_preview=options["disable_preview"],
            offline=options["offline"],
            remote=options["remote"],
            remote_url=options["remote_url"],
            remote_port=options["remote_port"],
            remote_tls=options["remote_tls"],
            remote_shared_secret=options["remote_shared_secret"],
            cloud_compute=options["cloud_compute"],
            api_key=options["api_key"],
            cloud_api_base_url=options["cloud_api_base_url"],
        )

    def execute(self, arguments: DrawThingsGenerateArguments, *, dry_run: bool, timeout: float | None, shutdown_grace: float) -> GenerationOutcome:
        """Preview or execute a prepared request."""
        if timeout is not None and timeout <= 0:
            raise ValueError("--timeout must be positive")
        if shutdown_grace < 0:
            raise ValueError("--shutdown-grace must not be negative")
        if dry_run:
            return GenerationOutcome(exit_code=0, command_preview=self._format_preview(arguments.command))
        if self._find_executable(arguments.executable) is None:
            raise ValueError(f"Could not find '{arguments.executable}' on PATH. Install Draw Things CLI or pass --executable with its path.")
        result = self._runner_factory(arguments, timeout, shutdown_grace).run()
        return GenerationOutcome(
            exit_code=self._exit_code(result),
            timed_out=result.timed_out,
            termination_signal=result.termination_signal,
        )

    @staticmethod
    def _exit_code(result: RunResult) -> int:
        """Map a run to a shell exit code by cause, not by the child's own code."""
        if result.timed_out:
            return 124
        if result.termination_signal is not None:
            # Stopped by the wrapper: report the signal even if the child exited 0.
            return 128 + result.termination_signal.value
        if result.return_code < 0:
            # Killed by a signal from outside the wrapper.
            return 128 - result.return_code
        return result.return_code

    @staticmethod
    def _input_file(path: Path, name: str) -> Path:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise ValueError(f"{name} does not exist or is not a file: {resolved}")
        return resolved

    @classmethod
    def _prompt_file(cls, path: str | None, name: str) -> Path | str | None:
        if path is None or path == "-":
            return path
        return cls._input_file(Path(path), name)

    @staticmethod
    def redact_command(command: tuple[str, ...]) -> list[str]:
        """Return the command with credential values replaced, safe to show or save."""
        display = list(command)
        for index, token in enumerate(display[:-1]):
            if token in {"--api-key", "--remote-shared-secret"}:
                display[index + 1] = "[redacted]"
        return display

    @classmethod
    def _format_preview(cls, command: tuple[str, ...]) -> str:
        return shlex.join(cls.redact_command(command))
