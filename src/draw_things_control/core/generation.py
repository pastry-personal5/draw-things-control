"""Prepare and execute generation requests independently of the CLI."""

from __future__ import annotations

import json
import shlex
import signal
from collections.abc import Callable, Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from draw_things_control.core.arguments import DEFAULT_VIDEO_FORMAT, DrawThingsGenerateArguments, redact_command
from draw_things_control.core.errors import InputError, ToolMissingError
from draw_things_control.core.exit_codes import EXIT_TIMEOUT, exit_code_for_child_signal, exit_code_for_signal
from draw_things_control.core.process.output import MessageCallback
from draw_things_control.core.process.runner import ChildStartCallback, Runner, RunnerFactory, RunResult


@dataclass(frozen=True)
class GenerationOutcome:
    """User-facing outcome without subprocess implementation details."""

    exit_code: int
    command_preview: str | None = None
    timed_out: bool = False
    termination_signal: signal.Signals | None = None


def require_executable(find_executable: Callable[[str], str | None], executable: str) -> str:
    """The path of ``executable``, or a ToolMissingError that says how to fix it."""
    path = find_executable(executable)
    if path is None:
        raise ToolMissingError(f"Could not find '{executable}' on PATH. Install Draw Things CLI or pass --executable with its path.")
    return path


@dataclass(frozen=True)
class GenerateRequest:
    """What the ``generate`` command was asked to do, as typed: unset options are None, false, or empty."""

    models_dir: Path | None = None
    model: str | None = None
    prompt: str | None = None
    prompt_file: str | None = None
    negative_prompt: str | None = None
    negative_prompt_file: str | None = None
    steps: int | None = None
    cfg: float | None = None
    width: int | None = None
    height: int | None = None
    frames: int | None = None
    strength: float | None = None
    seed: int | None = None
    config_json: str | None = None
    config_file: Path | None = None
    image: Sequence[Path] = ()
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
    executable: str = "draw-things-cli"


# The options that are resolved (files found, settings merged) before they reach the arguments; every other option passes through.
RESOLVED_OPTIONS = frozenset({"models_dir", "model", "prompt_file", "negative_prompt_file", "config_json", "config_file", "image", "audio", "output"})


class GenerationService:
    """Resolve inputs, create Draw Things arguments, and run one request."""

    def __init__(
        self,
        runner_factory: RunnerFactory[Runner],
        find_executable: Callable[[str], str | None],
        config_loader: Callable[[Path], dict[str, Any]],
    ) -> None:
        self._runner_factory = runner_factory
        self._find_executable = find_executable
        self._config_loader = config_loader

    def prepare(self, request: GenerateRequest) -> DrawThingsGenerateArguments:
        """Validate files and config, then construct typed CLI arguments."""
        config_json, model = self._resolve_configuration(request)
        images = tuple(self._input_file(image, "Input image") for image in request.image)
        output = request.output.expanduser() if request.output is not None else None
        if output is not None and not output.parent.is_dir():
            raise InputError(f"Output directory does not exist: {output.parent}")
        passed = {field.name: getattr(request, field.name) for field in fields(request) if field.name not in RESOLVED_OPTIONS}
        # A .mov output is ProRes 4444 unless --video-format says otherwise (owner decision); .mp4 is passed on as asked.
        if passed.get("video_format") is None and output is not None and output.suffix.lower() == ".mov":
            passed["video_format"] = DEFAULT_VIDEO_FORMAT
        return DrawThingsGenerateArguments(
            **passed,
            model=model,
            models_dir=request.models_dir.expanduser() if request.models_dir is not None else None,
            prompt_file=self._prompt_file(request.prompt_file, "Prompt file"),
            negative_prompt_file=self._prompt_file(request.negative_prompt_file, "Negative prompt file"),
            config_json=config_json,
            image=images[0] if images else None,
            reference_images=images[1:],
            audio=self._input_file(request.audio, "Audio file") if request.audio is not None else None,
            output=output,
        )

    def _resolve_configuration(self, request: GenerateRequest) -> tuple[str | None, str]:
        """The ``--config-json`` text to pass, and the model.

        draw-things-cli reads only JSON, so a configuration file is passed inline, with ``--config-json`` merged on top.
        """
        config_json = request.config_json
        settings: dict[str, Any] = {}
        if request.config_file is not None:
            # The format follows the name as given, as in validate-config, not the target of a symlink.
            settings = dict(self._config_loader(request.config_file.expanduser()))
        if request.config_json is not None:
            try:
                inline_settings = json.loads(request.config_json)
            except json.JSONDecodeError as error:
                raise InputError(f"--config-json is not valid JSON: {error.msg}") from error
            if not isinstance(inline_settings, dict):
                raise InputError("--config-json must contain a JSON object")
            settings.update(inline_settings)
        if request.config_file is not None:
            config_json = json.dumps(settings, separators=(",", ":"))
        model = request.model or settings.get("model")
        if not model:
            raise InputError("A model must be set in the configuration or passed with --model")
        return config_json, str(model)

    def execute(self, arguments: DrawThingsGenerateArguments, *, dry_run: bool, timeout: float | None, shutdown_grace: float, on_message: MessageCallback | None = None, on_start: ChildStartCallback | None = None) -> GenerationOutcome:
        """Preview or execute a prepared request; ``on_message`` receives each line the child prints, ``on_start`` its PID and name."""
        if timeout is not None and timeout <= 0:
            raise InputError("--timeout must be positive")
        if shutdown_grace < 0:
            raise InputError("--shutdown-grace must not be negative")
        if dry_run:
            return GenerationOutcome(exit_code=0, command_preview=self._format_preview(arguments.command))
        require_executable(self._find_executable, arguments.executable)
        result = self._runner_factory(arguments, timeout, shutdown_grace, on_message, on_start).run()
        return GenerationOutcome(
            exit_code=self._exit_code(result),
            timed_out=result.timed_out,
            termination_signal=result.termination_signal,
        )

    @staticmethod
    def _exit_code(result: RunResult) -> int:
        """Map a run to a shell exit code by cause, not by the child's own code."""
        if result.timed_out:
            return EXIT_TIMEOUT
        if result.termination_signal is not None:
            # Stopped by the wrapper: report the signal even if the child exited 0.
            return exit_code_for_signal(result.termination_signal)
        if result.return_code < 0:
            # Killed by a signal from outside the wrapper.
            return exit_code_for_child_signal(result.return_code)
        return result.return_code

    @staticmethod
    def _input_file(path: Path, name: str) -> Path:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise InputError(f"{name} does not exist or is not a file: {resolved}")
        return resolved

    @classmethod
    def _prompt_file(cls, path: str | None, name: str) -> Path | str | None:
        if path is None or path == "-":
            return path
        return cls._input_file(Path(path), name)

    @classmethod
    def _format_preview(cls, command: tuple[str, ...]) -> str:
        return shlex.join(redact_command(command))
