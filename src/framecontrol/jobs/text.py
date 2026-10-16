"""Describe jobs as text, for every front end; nothing here prints or exits, and only report_ignored_config logs."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from loguru import logger

from framecontrol.core.backend_ids import H3_BACKEND
from framecontrol.core.cooldown import CooldownPolicy
from framecontrol.jobs.definition import ColorPolicy, EffectiveRun, JobDefinition, PromptPair

if TYPE_CHECKING:
    from framecontrol.jobs.events import MediaChecked
    from framecontrol.jobs.planning import JobPreview, PlannedRun

# The seed a front end shows in a plan for a job with no configured seed, so the plan is the same every time it is shown.
PLACEHOLDER_SEED = 0
RANDOM_SEED_TEXT = "random (drawn when the job starts)"
PLACEHOLDER_SEED_NOTE = f"The job sets no seed: the commands use the placeholder seed {PLACEHOLDER_SEED}, and a run replaces it with a seed drawn when the job starts."


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


def share_text(ratio: float) -> str:
    """An auto cooldown's ratio as a share: ``half``, or a percentage such as ``40%``."""
    if ratio == 0.5:
        return "half"
    text = format(Decimal(repr(float(ratio))) * 100, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text}%"


def bounds_text(policy: CooldownPolicy) -> str:
    """An auto cooldown's bounds, for example ``5 min to 30 min``."""
    return f"{duration_text(policy.minimum_seconds)} to {duration_text(policy.maximum_seconds)}"


def policy_text(policy: CooldownPolicy) -> str:
    """A cooldown in a few words, as history shows it: ``900 s``, ``off``, or ``auto, half, 5 min to 30 min``."""
    if policy.mode == "auto":
        return f"auto, {share_text(policy.ratio)}, {bounds_text(policy)}"
    if policy.mode == "manual":
        return seconds_text(policy.seconds)
    return "off"


def cooldown_summary(policy: CooldownPolicy, source: str, source_prefix: str = "") -> str:
    """A cooldown and its source, for example ``cooldown 900 s (from global_config)``."""
    where = f"({source_prefix}{source})"
    if policy.mode == "auto":
        return f"cooldown auto, {share_text(policy.ratio)} of each run, {bounds_text(policy)} {where}"
    if policy.mode == "manual" and policy.seconds > 0:
        return f"cooldown {seconds_text(policy.seconds)} {where}"
    return f"no cooldown {where}"


def cooldown_details(job: JobDefinition) -> str:
    """The cooldown line of validate-job: the mode or value, its source, and the waits it adds."""
    policy, waits = job.cooldown, job.run_count - 1
    if policy.mode == "off":
        return f"off ({job.cooldown_source})"
    if policy.mode == "manual" and policy.seconds <= 0:
        return f"none ({job.cooldown_source})"
    plural = "s" if waits > 1 else ""
    if policy.mode == "auto":
        extent = "no waits: 1 run" if waits == 0 else f"up to {waits} wait{plural}, {duration_text(waits * policy.maximum_seconds)} total at most"
        return f"auto: {share_text(policy.ratio)} of each run's time, {bounds_text(policy)}, from {job.cooldown_source} ({extent})"
    extent = "no waits: 1 run" if waits == 0 else f"{waits} wait{plural}, {duration_text(waits * policy.seconds)} total"
    return f"{seconds_text(policy.seconds)} between runs, from {job.cooldown_source} ({extent})"


def planned_cooldown_text(policy: CooldownPolicy, after_run: int) -> str | None:
    """The dry-run plan's text for the wait after run ``after_run``, without the ``# `` the CLI adds; None when there is no wait."""
    if policy.mode == "auto":
        return f"Cooldown auto: {share_text(policy.ratio)} of run {after_run}'s time, {bounds_text(policy)}"
    if policy.mode == "manual" and policy.seconds > 0:
        return f"Cooldown {seconds_text(policy.seconds)}"
    return None


def auto_wait_text(seconds: float, ratio: float, after_run: int, run_seconds: float, bound: str | None, *, commas: bool = False) -> str:
    """An auto wait and why it is that long: ``12 min (half of run 1's 24 min)``, or with ``commas``,
    ``12 min, half of run 1's 24 min,``; a bound is named: ``5 min (the minimum; half of run 1's 3 min is less)``."""
    share = f"{share_text(ratio)} of run {after_run}'s {duration_text(run_seconds)}"
    if bound is not None:
        return f"{duration_text(seconds)} (the {bound}; {share} is {'less' if bound == 'minimum' else 'more'})"
    return f"{duration_text(seconds)}, {share}," if commas else f"{duration_text(seconds)} ({share})"


def ignored_config_lines(job: JobDefinition) -> list[str]:
    """The base configuration keys the job's mode or size ignores, and how run 1's input is resized, one line each."""
    if job.effective_runs:
        return _ignored_config_lines_v2(job)
    lines = [f"Ignoring {key} ({value}) from config_file {job.config_file}: not used in {job.mode} jobs; the job's run_count sets the number of runs" for key, value in job.ignored_config.items()]
    if job.size is not None:
        size = f"{job.size[0]}x{job.size[1]}"
        for source, key, value in job.ignored_size:
            if source == "config_file":
                lines.append(f"Ignoring {key} ({value}) from config_file {job.config_file}: desired_input_width/desired_input_height set the size ({size})")
            else:
                lines.append(f"Ignoring config_override.{key} ({value}): desired_input_width/desired_input_height set the size ({size})")
    plan = job.input_copy
    if plan is not None and job.input is not None:
        lines.append(plan.describe(job.input.name))
    return lines


def _ignored_config_lines_v2(job: JobDefinition) -> list[str]:
    """What each version-2 settings profile's base configuration is not used for, once per profile, then run 1's input."""
    lines: list[str] = []
    seen: set[str] = set()
    for run in job.effective_runs:
        if run.settings_profile in seen:
            continue
        seen.add(run.settings_profile)
        view = run.backend_job
        where = f"config_file {view.config_file} (settings profile {run.settings_profile})"
        for key, value in view.ignored_config.items():
            if key == "seed":
                lines.append(f"Ignoring seed ({value}) from {where}: a run's seed is its knobs.seed, else the execution seed")
            else:
                lines.append(f"Ignoring {key} ({value}) from {where}: not used in {run.pipeline} runs; the job's runs set the number of runs")
        if view.size is not None:
            size = f"{view.size[0]}x{view.size[1]}"
            for source, key, value in view.ignored_size:
                origin = where if source == "config_file" else f"config_override.{key} of settings profile {run.settings_profile}"
                lines.append(f"Ignoring {key if source == 'config_file' else 'value'} ({value}) from {origin}: input_resize sets the size ({size})")
    plan = job.input_copy
    if plan is not None and job.input is not None:
        lines.append(plan.describe(job.input.name))
    return lines


def report_ignored_config(job: JobDefinition) -> None:
    """Log ignored_config_lines, for the CLI and the job's log; a front end that owns the terminal shows the lines instead."""
    for line in ignored_config_lines(job):
        logger.info("{}", line)


MEDIA_CHECK_LABELS = {"input": "Input check", "resized_input": "Resized input check", "output": "Output check", "last_frame": "Last frame check", "color_drift": "Color drift check", "color_correction": "Color correction check"}


def media_check_text(event: MediaChecked) -> str:
    """The line the job log, ``fc serve``'s output, and the TUI's Messages print for a media check."""
    return f"{MEDIA_CHECK_LABELS.get(event.stage, event.stage)} (run {event.run}): {media_check_result(event.file, event.summary, event.verdict, event.notes)}"


def media_check_result(file: str, summary: str, verdict: str, notes: tuple[str, ...]) -> str:
    """What a media check found, after its label: the file, what it holds, the verdict, and any notes."""
    line = f"{file}: {summary}: {verdict}"
    return f"{line}. {' '.join(notes)}" if notes else line


def job_summary(job: JobDefinition, *, random_seed_text: str | None = None) -> list[tuple[str, str]]:
    """The (label, value) rows validate-job prints under ``Valid job:``; ``random_seed_text``, if given, is the whole seed value when the job sets none."""
    seed, source = job.configured_seed()
    seed_value = random_seed_text if seed is None and random_seed_text is not None else f"{seed if seed is not None else '(random, drawn when the job starts)'} ({source})"
    color_summary = color_text(job.color) if job.color.corrects else "none, measure only"
    if job.backend == H3_BACKEND:
        color_summary += " (H3 policy v1)"
    if job.effective_runs:
        return [
            ("name", job.name),
            ("format", "version 2"),
            ("backend", job.backend),
            ("mode", job.mode_label),
            ("runs", f"{job.run_count} ({', '.join(pair.name for pair in job.schedule())})"),
            ("cooldown", cooldown_details(job)),
            ("input", str(job.input or "(none)")),
            ("output directory", str(job.output_directory)),
            ("execution seed", seed_value),
            *[(f"run {run.number}", run_text(run)) for run in job.effective_runs],
        ]
    rows = [
        ("name", job.name),
        ("backend", job.backend),
        ("mode", str(job.mode)),
        ("runs", f"{job.run_count} ({', '.join(pair.name for pair in job.schedule())})"),
        ("cooldown", cooldown_details(job)),
        ("input", str(job.input or "(none, text only)")),
        ("output directory", str(job.output_directory)),
        *([("output format", f"{job.video_format} (.{job.extension})")] if job.video_format is not None else []),
        *([("color", color_summary)] if job.color.corrects or job.backend == H3_BACKEND else []),
        ("config file", job.config_file),
        ("model", job.model),
        ("seed", seed_value),
    ]
    if job.backend == H3_BACKEND and job.backend_data is not None:
        from framecontrol.jobs.backends.h3 import H3JobData

        if isinstance(job.backend_data, H3JobData):
            settings = job.backend_data.settings
            controls = [f"{settings.width}x{settings.height}", f"{settings.seconds:g}s/{settings.frames} requested frames", f"{settings.effective_frames} effective frames", f"{settings.steps} steps", f"seed {settings.seed}"]
            if settings.render_width is not None:
                controls.append(f"render {settings.render_width}x{settings.render_height}")
            controls.extend(f"{run.run}:{run.pipeline}" + (f" ({run.reference_pair})" if run.reference_pair else "") for run in job.backend_data.runs)
            rows.append(("H3", "; ".join(controls)))
    return rows


def knobs_text(knobs: dict[str, object], sources: dict[str, str]) -> str:
    """A run's resolved knobs and where each came from: ``seconds 5 (defaults), seed 42 (run)``."""
    parts = []
    for key, value in sorted(knobs.items()):
        shown = f"{value:g}" if isinstance(value, float) else str(value)
        parts.append(f"{key} {shown}" + (f" ({sources[key]})" if key in sources else ""))
    return ", ".join(parts)


def run_text(run: EffectiveRun) -> str:
    """One compiled run in a line: its backend and pipeline, prompt pair, profile, duration, seed, and time limit."""
    knobs = run.knobs
    parts = [f"{run.backend}/{run.pipeline}", f"pair {run.prompt_pair}", f"profile {run.settings_profile}"]
    if run.backend == H3_BACKEND:
        parts.append(f"{knobs['seconds']:g}s requested, {knobs['requested_frames']} frames requested, {knobs['effective_frames']} effective")
        if "reference_pair" in knobs:
            parts.append(f"references {knobs['reference_pair']}")
    elif "frames" in knobs:
        parts.append(f"{knobs['frames']} frames" + (f" ({knobs['seconds']:g}s at {knobs['nominal_fps']:g} fps)" if "seconds" in knobs else ""))
    parts.append(f"seed {knobs['seed']}" if "seed" in knobs else "execution seed")
    parts.append(f"time limit {duration_text(float(knobs['timeout_seconds']))}")
    return ", ".join(parts)


def color_text(policy: ColorPolicy) -> str:
    """A correcting job's color policy in one line, for example ``blend, first 0.25, reanchor on prompt pair, regions``."""
    parts = [policy.anchor]
    if policy.anchor == "blend":
        parts.append(f"first {policy.first_weight:g}")
    if policy.holds_to_anchor:
        parts.append("reanchor on prompt pair" if policy.reanchor == "prompt_pair" else "never reanchor")
    if policy.strength != 1:
        parts.append(f"strength {policy.strength:g}")
    parts.append("regions" if policy.regions else "whole frame")
    return ", ".join(parts)


def pair_runs(job: JobDefinition) -> list[tuple[PromptPair, tuple[int, ...]]]:
    """Each prompt pair and the runs that use it, in the job's order; a pair no run uses gets an empty tuple."""
    schedule = job.schedule()
    return [(pair, tuple(number for number, used in enumerate(schedule, start=1) if used is pair)) for pair in job.prompt_pairs]


@dataclass(frozen=True)
class PlanStep:
    """One run of the dry-run plan, as text without the ``# `` the CLI adds: the wait before it (None when there is none),
    its heading, the planned run, and its redacted command as arguments."""

    cooldown: str | None
    heading: str
    run: PlannedRun
    command: tuple[str, ...]


def plan_header(job: JobDefinition, preview: JobPreview) -> list[str]:
    """The dry-run plan's lines before its runs, as text without the ``# `` the CLI adds."""
    return [
        f"Job {job.name} ({job.mode_label}, {job.backend}): {len(preview.runs)} runs, seed {preview.seed} ({preview.seed_source}), {cooldown_summary(job.cooldown, job.cooldown_source)}",
        "Output names are examples; a real run generates new ones.",
    ]


def plan_steps(job: JobDefinition, preview: JobPreview) -> list[PlanStep]:
    """Each run of the dry-run plan, in order; the TUI reads the plan from here."""
    from framecontrol.jobs.backends.h3 import H3JobData

    result = []
    for run, command in zip(preview.runs, preview.commands, strict=True):
        detail = f"Run {run.number}/{len(preview.runs)} (pair {run.pair.name})"
        effective = job.effective_run(run.number)
        if effective is not None:
            detail += f" ({effective.backend}/{effective.pipeline}, profile {effective.settings_profile})"
        elif job.backend == H3_BACKEND and isinstance(job.backend_data, H3JobData):
            h3_run = job.backend_data.runs[run.number - 1]
            detail += f" ({h3_run.pipeline}" + (f", references {h3_run.reference_pair})" if h3_run.reference_pair else ")")
        result.append(PlanStep(planned_cooldown_text(job.cooldown, run.number - 1) if run.number > 1 else None, detail, run, command))
    return result
