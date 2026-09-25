"""Read jobs and describe them as text, for every front end; nothing here prints or exits, and only report_ignored_config logs."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from draw_things_control.core.global_config import GlobalConfig, load_global_config
from draw_things_control.jobs.job_definition import JobDefinition, PromptPair, load_job

if TYPE_CHECKING:
    from draw_things_control.jobs.job_service import JobPreview

# The seed a front end shows in a plan for a job with no configured seed, so the plan is the same every time it is shown.
PLACEHOLDER_SEED = 0
RANDOM_SEED_TEXT = "random (drawn when the job starts)"
PLACEHOLDER_SEED_NOTE = f"The job sets no seed: the commands use the placeholder seed {PLACEHOLDER_SEED}, and a run replaces it with a seed drawn when the job starts."


def read_settings(global_config: Path) -> GlobalConfig:
    """Load the global configuration; raises ValueError if it is invalid."""
    return load_global_config(global_config.expanduser())


def read_job(job_file: Path, global_config: Path | GlobalConfig, *, decode_input: bool = True) -> tuple[JobDefinition, GlobalConfig]:
    """Load the job and the global configuration (a path, or one already loaded); raises ValueError if either is invalid."""
    settings = global_config if isinstance(global_config, GlobalConfig) else read_settings(global_config)
    return load_job(job_file, settings, decode_input=decode_input), settings


def job_files(directory: Path) -> list[Path]:
    """The ``*.yaml`` and ``*.yml`` files (in any letter case) directly in ``directory``, by file name; dotfiles and sub-directories are skipped.

    Raises OSError if the directory cannot be read.
    """
    return sorted((path for path in directory.iterdir() if path.suffix.lower() in {".yaml", ".yml"} and not path.name.startswith(".") and path.is_file()), key=lambda path: path.name)


def seconds_text(seconds: float) -> str:
    """A number of seconds as written in the job, for example ``900 s``, ``0.5 s``, or ``0.00001 s``."""
    # repr gives the shortest digits that round-trip; Decimal writes them without an exponent.
    text = format(Decimal(repr(float(seconds))), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text} s"


def duration_text(seconds: float) -> str:
    """A length of time in hours, minutes, and seconds to a tenth, for example ``1 h 30 min`` or ``0.4 s``."""
    total = round(seconds, 1)
    hours, rest = divmod(int(total), 3600)
    minutes = rest // 60
    secs = round(total - hours * 3600 - minutes * 60, 1)
    parts = [f"{value} {unit}" for value, unit in ((hours, "h"), (minutes, "min")) if value]
    if secs or not parts:
        parts.append(seconds_text(secs))
    return " ".join(parts)


def cooldown_summary(job: JobDefinition, source_prefix: str = "") -> str:
    """The job's cooldown and its source, for example ``cooldown 900 s (from global_config)``."""
    if job.cooldown_seconds > 0:
        return f"cooldown {seconds_text(job.cooldown_seconds)} ({source_prefix}{job.cooldown_source})"
    return f"no cooldown ({source_prefix}{job.cooldown_source})"


def cooldown_details(job: JobDefinition) -> str:
    """The cooldown line of validate-job: the value, its source, and the waits it adds."""
    if job.cooldown_seconds <= 0:
        return f"none ({job.cooldown_source})"
    waits = job.run_count - 1
    if waits == 0:
        extent = "no waits: 1 run"
    else:
        extent = f"{waits} wait{'s' if waits > 1 else ''}, {duration_text(waits * job.cooldown_seconds)} total"
    return f"{seconds_text(job.cooldown_seconds)} between runs, from {job.cooldown_source} ({extent})"


def ignored_config_lines(job: JobDefinition) -> list[str]:
    """The base configuration keys the job's mode or size ignores, and how run 1's input is resized, one line each."""
    lines = [f"Ignoring {key} ({value}) from config_file {job.config_file}: not used in {job.mode} jobs; the job's run_count sets the number of runs" for key, value in job.ignored_config.items()]
    if job.size is not None:
        size = f"{job.size[0]}x{job.size[1]}"
        for source, key, value in job.ignored_size:
            if source == "config_file":
                lines.append(f"Ignoring {key} ({value}) from config_file {job.config_file}: desired_input_width/desired_input_height set the size ({size})")
            else:
                lines.append(f"Ignoring config_override.{key} ({value}): desired_input_width/desired_input_height set the size ({size})")
    if job.input_resize is not None and job.input is not None:
        lines.append(job.input_resize.describe(job.input.name))
    return lines


def report_ignored_config(job: JobDefinition) -> None:
    """Log ignored_config_lines, for the CLI and the job's log; a front end that owns the terminal shows the lines instead."""
    for line in ignored_config_lines(job):
        logger.info("{}", line)


def job_summary(job: JobDefinition, *, random_seed_text: str | None = None) -> list[tuple[str, str]]:
    """The (label, value) rows validate-job prints under ``Valid job:``; ``random_seed_text``, if given, is the whole seed value when the job sets none."""
    seed, source = job.configured_seed()
    seed_value = random_seed_text if seed is None and random_seed_text is not None else f"{seed if seed is not None else '(random, drawn when the job starts)'} ({source})"
    return [
        ("name", job.name),
        ("mode", str(job.mode)),
        ("runs", f"{job.run_count} ({', '.join(pair.name for pair in job.schedule())})"),
        ("cooldown", cooldown_details(job)),
        ("input", str(job.input or "(none, text only)")),
        ("output directory", str(job.output_directory)),
        ("config file", job.config_file),
        ("model", job.model),
        ("seed", seed_value),
    ]


def pair_runs(job: JobDefinition) -> list[tuple[PromptPair, tuple[int, ...]]]:
    """Each prompt pair and the runs that use it, in the job's order; a pair no run uses gets an empty tuple."""
    schedule = job.schedule()
    return [(pair, tuple(number for number, used in enumerate(schedule, start=1) if used is pair)) for pair in job.prompt_pairs]


def plan_lines(job: JobDefinition, preview: JobPreview) -> list[str]:
    """The lines run-job --dry-run prints: a header, then each run's comment and redacted command."""
    lines = [
        f"# Job {job.name} ({job.mode}): {len(preview.runs)} runs, seed {preview.seed} ({preview.seed_source}), {cooldown_summary(job)}",
        "# Output names are examples; a real run generates new ones.",
    ]
    for run, command in zip(preview.runs, preview.command_previews, strict=True):
        if run.number > 1 and job.cooldown_seconds > 0:
            lines.append(f"# Cooldown {seconds_text(job.cooldown_seconds)}")
        lines.append(f"# Run {run.number}/{len(preview.runs)} (pair {run.pair.name})")
        lines.append(command)
    return lines
