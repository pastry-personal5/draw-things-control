"""Compile a version-2 job file: catalogs, scoped defaults, and ordered runs become immutable per-run adapter views.

Each run is resolved on its own (backend, pipeline, prompt pair, settings profile, knobs), validated by its own backend
adapter through a one-run job view, and frozen in an ``EffectiveRun``. Nothing here reads the previous run's settings:
the only cross-run facts are the visual dependency and the size of what the previous run produces.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from framecontrol.core.backend_ids import DRAW_THINGS_BACKEND, H3_BACKEND, KNOWN_BACKENDS
from framecontrol.core.cooldown import replaced_cooldown_message
from framecontrol.core.errors import InputError
from framecontrol.core.numbers import is_int, is_number
from framecontrol.jobs.definition import ColorPolicy, ConfigOverride, EffectiveRun, JobDefinition, PromptPair
from framecontrol.jobs.overrides import MAX_SEED
from framecontrol.jobs.prompt_pairs import NAME_PATTERN

if TYPE_CHECKING:
    from framecontrol.core.cooldown import CooldownPolicy
    from framecontrol.jobs.backend import BackendRegistry
    from framecontrol.jobs.parsing import JobParser

JOB_KEYS = {"version", "name", "input", "prompt_pairs", "reference_pairs", "settings_profiles", "defaults", "runs", "output", "cooldown"}
REQUIRED_KEYS = ("version", "name", "prompt_pairs", "settings_profiles", "runs")
RUN_KEYS = {"backend", "pipeline", "prompt_pair", "settings_profile", "knobs"}
KNOB_KEYS = ("seconds", "frames", "seed", "timeout_seconds", "reference_pair")
BACKENDS = KNOWN_BACKENDS
PIPELINES = {DRAW_THINGS_BACKEND: ("i2i", "t2v", "i2v"), H3_BACKEND: ("i2av", "r2av")}
VISUAL_INPUT_PIPELINES = ("i2i", "i2v", "i2av")
TIMED_PIPELINES = ("i2v", "t2v", "i2av", "r2av")
H3_SETTINGS = {"width", "height", "steps", "render_width", "render_height", "denoise_reuse", "dit_layers", "core_reuse", "token_reduction", "ssd_streaming", "int8_row_fc2", "reference_rope", "reference_image_size", "compatibility", "color", "output"}
DRAW_THINGS_SETTINGS = {"config_file", "config_override", "output", "input_resize", "color"}
INPUT_RESIZE_KEYS = {"desired_width": "desired_input_width", "desired_height": "desired_input_height", "max_crop_percent": "max_input_crop_percent"}
H3_MIN_SECONDS, H3_MAX_SECONDS = 1, 15
VERSION_1_MESSAGE = "version 1 job files are no longer accepted. Rewrite the job as 'version: 2' with 'runs', 'settings_profiles', and 'defaults'; see docs/user-guide.md (Job files). Jobs already queued as version 1 keep running"
# Each key a version-1 file used, and where it lives in version 2.
REMOVED_KEYS = {
    "mode": "was removed in version 2; set 'pipeline' on each entry of 'runs'",
    "run_count": "was removed in version 2; the length of 'runs' is the run count",
    "backend": "was removed in version 2; set 'backend' on each entry of 'runs'",
    "h3_runs": "was removed in version 2; set 'backend: h3-metal-cli' and 'pipeline' on each entry of 'runs'",
    "backend_settings": "was removed in version 2; put these controls in a settings profile's 'settings'",
    "config_file": "was removed in version 2; set it in a draw-things-cli settings profile's 'settings.config_file'",
    "config_override": "was removed in version 2; set it in a draw-things-cli settings profile's 'settings.config_override'",
    "color": "was removed in version 2; set it in a settings profile's 'settings.color'",
    "run_timeout_seconds": "was removed in version 2; set 'knobs.timeout_seconds' on a run or in 'defaults'",
    "desired_input_width": "was removed in version 2; set it as 'desired_width' in a draw-things-cli settings profile's 'settings.input_resize'",
    "desired_input_height": "was removed in version 2; set it as 'desired_height' in a draw-things-cli settings profile's 'settings.input_resize'",
    "max_input_crop_percent": "was removed in version 2; set it as 'max_crop_percent' in a draw-things-cli settings profile's 'settings.input_resize'",
}
REMOVED_RUN_KEYS = {"run": "was removed in version 2; the position in 'runs' is the run number"}
REMOVED_OUTPUT_KEYS = {
    "extension": "was removed in version 2; set 'output.extension' in the settings profile",
    "video_format": "was removed in version 2; set 'output.video_format' in a draw-things-cli settings profile",
}


def job_format_summary(backends: BackendRegistry) -> dict[str, Any]:
    """What a client may write in a job file, for ``GET /capabilities``: the format version, each backend's pipelines,
    the knob names, and the Draw Things models whose frame rate is established (the only ones ``seconds`` can name)."""
    try:
        models = list(getattr(backends.get(DRAW_THINGS_BACKEND), "frame_rate_models", lambda: ())())
    except InputError:
        models = []
    return {"version": 2, "backends": {backend: list(PIPELINES[backend]) for backend in BACKENDS}, "knobs": list(KNOB_KEYS), "draw_things_seconds_models": models}


@dataclass(frozen=True)
class _Selection:
    """One run after its backend, pipeline, catalog names, and knobs are resolved, before its view is built."""

    number: int
    backend: str
    pipeline: str
    prompt: str
    profile: str
    knobs: dict[str, Any]
    sources: dict[str, str]


@dataclass(frozen=True)
class _Context:
    """What a nested backend error needs to name the version-2 field a version-1 key came from."""

    profile: str
    run_field: str
    reference: str | None


@dataclass(frozen=True)
class _Catalogs:
    prompts: dict[str, PromptPair]
    references: dict[str, dict[str, Any]]
    profiles: dict[str, tuple[str, dict[str, Any]]]


@dataclass(frozen=True)
class _RootOptions:
    output_directory: Path
    cooldown: CooldownPolicy
    cooldown_source: str


class RunCompiler:
    """Turns a version-2 mapping into a ``JobDefinition`` whose ``effective_runs`` are its frozen per-run plan."""

    def __init__(self, parser: JobParser) -> None:
        self._parser = parser
        self._views: dict[Any, JobDefinition] = {}

    def compile(self, data: dict[str, Any], source_text: str) -> JobDefinition:
        parser = self._parser
        self._check_top_level(data)
        name = parser.job_name(data["name"])
        catalogs = self._catalogs(data)
        defaults = self._defaults(data.get("defaults", []), catalogs.prompts, catalogs.references, catalogs.profiles)
        raw_runs = data["runs"]
        if not isinstance(raw_runs, list) or not raw_runs:
            parser.fail("runs", "must be a nonempty list")
        options = self._root_options(data, name)
        selections = [self._select(index, item, catalogs.prompts, catalogs.references, catalogs.profiles, defaults) for index, item in enumerate(raw_runs)]
        self._check_all_used(selections, defaults, catalogs.prompts, catalogs.references, catalogs.profiles)
        input_path = self._input(data.get("input"), selections[0].pipeline)
        effective = self._resolve_runs(selections, name, catalogs, input_path, options.output_directory, source_text)
        self._check_edges(effective)
        return self._root(name, effective, input_path, catalogs.prompts, options.output_directory, options.cooldown, options.cooldown_source, source_text)

    # --- catalogs -------------------------------------------------------------------------------------------------

    def _catalogs(self, data: dict[str, Any]) -> _Catalogs:
        return _Catalogs(self._prompts(data["prompt_pairs"]), self._references(data.get("reference_pairs", {})), self._profiles(data["settings_profiles"]))

    def _check_top_level(self, data: dict[str, Any]) -> None:
        parser = self._parser
        if "cooldown_seconds" in data:
            raise InputError(f"{parser.path}: {replaced_cooldown_message(data['cooldown_seconds'])}", field="cooldown_seconds", path=parser.path)
        for key in data:
            if key in REMOVED_KEYS:
                parser.fail(key, REMOVED_KEYS[key])
        parser.check_keys(data, JOB_KEYS, "")
        for key in REQUIRED_KEYS:
            if key not in data:
                parser.fail(key, "is required")

    def _catalog_name(self, catalog: str, name: Any) -> str:
        if not isinstance(name, str) or not NAME_PATTERN.match(name):
            self._parser.fail(catalog, f"names must be 1 to 64 lowercase letters, digits, and hyphens ({name!r})")
        return name

    def _prompts(self, value: Any) -> dict[str, PromptPair]:
        parser = self._parser
        if not isinstance(value, dict) or not value:
            parser.fail("prompt_pairs", "must be a nonempty mapping of name to prompt pair")
        result: dict[str, PromptPair] = {}
        for raw_name, item in value.items():
            name = self._catalog_name("prompt_pairs", raw_name)
            field = f"prompt_pairs.{name}"
            if not isinstance(item, dict):
                parser.fail(field, "must be a mapping")
            parser.check_keys(item, {"positive", "negative"}, f"{field}.")
            positive, negative = item.get("positive"), item.get("negative")
            if not isinstance(positive, str) or not positive.strip():
                parser.fail(f"{field}.positive", "is required and must be non-empty text")
            if negative is not None and not isinstance(negative, str):
                parser.fail(f"{field}.negative", "must be text")
            result[name] = PromptPair(name, positive, negative, (), False)
        return result

    def _references(self, value: Any) -> dict[str, dict[str, Any]]:
        parser = self._parser
        if not isinstance(value, dict):
            parser.fail("reference_pairs", "must be a mapping of name to reference pair")
        result: dict[str, dict[str, Any]] = {}
        for raw_name, item in value.items():
            name = self._catalog_name("reference_pairs", raw_name)
            if not isinstance(item, dict):
                parser.fail(f"reference_pairs.{name}", "must be a mapping")
            parser.check_keys(item, {"references"}, f"reference_pairs.{name}.")
            if "references" not in item:
                parser.fail(f"reference_pairs.{name}.references", "is required")
            result[name] = item
        return result

    def _profiles(self, value: Any) -> dict[str, tuple[str, dict[str, Any]]]:
        parser = self._parser
        if not isinstance(value, dict) or not value:
            parser.fail("settings_profiles", "must be a nonempty mapping of name to settings profile")
        result: dict[str, tuple[str, dict[str, Any]]] = {}
        for raw_name, item in value.items():
            name = self._catalog_name("settings_profiles", raw_name)
            field = f"settings_profiles.{name}"
            if not isinstance(item, dict):
                parser.fail(field, "must be a mapping")
            parser.check_keys(item, {"backend", "settings"}, f"{field}.")
            for key in ("backend", "settings"):
                if key not in item:
                    parser.fail(f"{field}.{key}", "is required")
            backend, settings = item["backend"], item["settings"]
            if backend not in BACKENDS:
                parser.fail(f"{field}.backend", f"must be {' or '.join(BACKENDS)}")
            if not isinstance(settings, dict):
                parser.fail(f"{field}.settings", "must be a mapping")
            allowed = H3_SETTINGS if backend == H3_BACKEND else DRAW_THINGS_SETTINGS
            for key in ("frames", "seconds", "seed"):
                if key in settings:
                    parser.fail(f"{field}.settings.{key}", "is a run knob in version 2; set it under 'knobs'")
            parser.check_keys(settings, allowed, f"{field}.settings.")
            if backend == H3_BACKEND:
                for key in ("width", "height", "steps"):
                    if key not in settings:
                        parser.fail(f"{field}.settings.{key}", "is required")
            else:
                if "config_file" not in settings:
                    parser.fail(f"{field}.settings.config_file", "is required")
                override = settings.get("config_override", {})
                if isinstance(override, dict):
                    for key in ("frame_count", "seed"):
                        if key in override:
                            parser.fail(f"{field}.settings.config_override.{key}", "is a run knob in version 2; set 'frames' or 'seed' under 'knobs'")
            result[name] = (backend, dict(settings))
        return result

    # --- defaults and knobs ---------------------------------------------------------------------------------------

    def _selector(self, backend: Any, pipeline: Any, field: str) -> tuple[str, str]:
        parser = self._parser
        if backend not in BACKENDS:
            parser.fail(f"{field}.backend", f"must be {' or '.join(BACKENDS)}")
        if pipeline not in PIPELINES[backend]:
            parser.fail(f"{field}.pipeline", f"must be {', '.join(PIPELINES[backend])} for {backend}")
        return backend, pipeline

    def _defaults(self, value: Any, prompts: dict[str, PromptPair], references: dict[str, dict[str, Any]], profiles: dict[str, tuple[str, dict[str, Any]]]) -> dict[tuple[str, str], dict[str, Any]]:
        parser = self._parser
        if not isinstance(value, list):
            parser.fail("defaults", "must be a list")
        result: dict[tuple[str, str], dict[str, Any]] = {}
        for index, item in enumerate(value):
            field = f"defaults[{index}]"
            if not isinstance(item, dict):
                parser.fail(field, "must be a mapping")
            parser.check_keys(item, RUN_KEYS, f"{field}.")
            if "backend" not in item or "pipeline" not in item:
                parser.fail(field, "must contain backend and pipeline")
            selector = self._selector(item["backend"], item["pipeline"], field)
            if selector in result:
                parser.fail(field, f"duplicates the defaults for {selector[0]}/{selector[1]}")
            self._check_names(item, selector[0], prompts, references, profiles, field)
            knobs = item.get("knobs", {})
            if not isinstance(knobs, dict):
                parser.fail(f"{field}.knobs", "must be a mapping")
            self._check_knobs(knobs, *selector, f"{field}.knobs", references)
            result[selector] = {**item, "knobs": dict(knobs)}
        return result

    def _check_names(self, item: dict[str, Any], backend: str, prompts: dict[str, PromptPair], references: dict[str, dict[str, Any]], profiles: dict[str, tuple[str, dict[str, Any]]], field: str) -> None:
        parser = self._parser
        for key, catalog, label in (("prompt_pair", prompts, "prompt pair"), ("settings_profile", profiles, "settings profile")):
            if key not in item:
                continue
            chosen = item[key]
            if not isinstance(chosen, str) or chosen not in catalog:
                parser.fail(f"{field}.{key}", f"selects the undeclared {label} {chosen!r}")
        if "settings_profile" in item and profiles[item["settings_profile"]][0] != backend:
            parser.fail(f"{field}.settings_profile", f"is a {profiles[item['settings_profile']][0]} profile, not a {backend} one")
        if "prompt_pair" in item and backend == H3_BACKEND and prompts[item["prompt_pair"]].negative is not None:
            parser.fail(f"prompt_pairs.{item['prompt_pair']}.negative", "is not supported by h3-metal-cli")

    def _applicable(self, backend: str, pipeline: str) -> set[str]:
        keys = {"seed", "timeout_seconds"}
        if pipeline in TIMED_PIPELINES:
            keys.add("seconds")
        if backend == DRAW_THINGS_BACKEND and pipeline in ("i2v", "t2v"):
            keys.add("frames")
        if backend == H3_BACKEND and pipeline == "r2av":
            keys.add("reference_pair")
        return keys

    def _check_knobs(self, knobs: dict[str, Any], backend: str, pipeline: str, field: str, references: dict[str, dict[str, Any]]) -> None:
        """Every check that needs only these knobs, for a run's or a defaults block's own mapping."""
        parser = self._parser
        parser.check_keys(knobs, set(KNOB_KEYS), f"{field}.")
        applicable = self._applicable(backend, pipeline)
        for key, value in knobs.items():
            if value is None:
                parser.fail(f"{field}.{key}", "must not be null; omit it to inherit")
            if key not in applicable:
                parser.fail(f"{field}.{key}", f"does not apply to {backend}/{pipeline}")
        if "seed" in knobs and (not is_int(knobs["seed"]) or not 0 <= knobs["seed"] <= MAX_SEED):
            parser.fail(f"{field}.seed", f"must be an integer from 0 to {MAX_SEED}")
        if "timeout_seconds" in knobs and not self._positive(knobs["timeout_seconds"]):
            parser.fail(f"{field}.timeout_seconds", "must be a positive, finite number of seconds")
        if "seconds" in knobs:
            if not self._positive(knobs["seconds"]):
                parser.fail(f"{field}.seconds", "must be a positive, finite number of seconds")
            if backend == H3_BACKEND and not H3_MIN_SECONDS <= float(knobs["seconds"]) <= H3_MAX_SECONDS:
                parser.fail(f"{field}.seconds", f"must be a finite number from {H3_MIN_SECONDS} to {H3_MAX_SECONDS} for h3-metal-cli")
        if "frames" in knobs and (not is_int(knobs["frames"]) or knobs["frames"] < 1):
            parser.fail(f"{field}.frames", "must be an integer >= 1")
        if "seconds" in knobs and "frames" in knobs:
            parser.fail(field, "cannot contain both seconds and frames")
        if "reference_pair" in knobs:
            chosen = knobs["reference_pair"]
            if not isinstance(chosen, str) or chosen not in references:
                parser.fail(f"{field}.reference_pair", f"selects the undeclared reference pair {chosen!r}")

    @staticmethod
    def _positive(value: Any) -> bool:
        return is_number(value) and math.isfinite(float(value)) and value > 0

    def _select(self, index: int, item: Any, prompts: dict[str, PromptPair], references: dict[str, dict[str, Any]], profiles: dict[str, tuple[str, dict[str, Any]]], defaults: dict[tuple[str, str], dict[str, Any]]) -> _Selection:
        parser = self._parser
        field = f"runs[{index}]"
        if not isinstance(item, dict):
            parser.fail(field, "must be a mapping")
        for key, message in REMOVED_RUN_KEYS.items():
            if key in item:
                parser.fail(f"{field}.{key}", message)
        parser.check_keys(item, RUN_KEYS, f"{field}.")
        if "backend" not in item or "pipeline" not in item:
            parser.fail(field, "must contain backend and pipeline")
        backend, pipeline = self._selector(item["backend"], item["pipeline"], field)
        block = defaults.get((backend, pipeline), {})
        self._check_names(item, backend, prompts, references, profiles, field)
        prompt = item.get("prompt_pair", block.get("prompt_pair"))
        profile = item.get("settings_profile", block.get("settings_profile"))
        if prompt is None:
            parser.fail(f"{field}.prompt_pair", "is required on the run or in the matching defaults")
        if profile is None:
            parser.fail(f"{field}.settings_profile", "is required on the run or in the matching defaults")
        if backend == H3_BACKEND and prompts[prompt].negative is not None:
            parser.fail(f"prompt_pairs.{prompt}.negative", "is not supported by h3-metal-cli")
        own = item.get("knobs", {})
        if not isinstance(own, dict):
            parser.fail(f"{field}.knobs", "must be a mapping")
        self._check_knobs(own, backend, pipeline, f"{field}.knobs", references)
        inherited = dict(block.get("knobs", {}))
        knobs, sources = dict(inherited), dict.fromkeys(inherited, "defaults")
        # A run's own temporal knob replaces the default's, whichever of seconds or frames each names.
        if "seconds" in own or "frames" in own:
            for key in ("seconds", "frames"):
                knobs.pop(key, None)
                sources.pop(key, None)
        for key, value in own.items():
            knobs[key], sources[key] = value, "run"
        self._check_resolved(knobs, backend, pipeline, field)
        return _Selection(index + 1, backend, pipeline, prompt, profile, knobs, sources)

    def _check_resolved(self, knobs: dict[str, Any], backend: str, pipeline: str, field: str) -> None:
        parser = self._parser
        if "timeout_seconds" not in knobs:
            parser.fail(f"{field}.knobs.timeout_seconds", "is required on the run or in the matching defaults")
        if backend == H3_BACKEND and "seconds" not in knobs:
            parser.fail(f"{field}.knobs.seconds", "is required on the run or in the matching defaults")
        if pipeline == "r2av" and "reference_pair" not in knobs:
            parser.fail(f"{field}.knobs.reference_pair", "is required on the run or in the matching defaults")

    def _check_all_used(self, selections: list[_Selection], defaults: dict[tuple[str, str], dict[str, Any]], prompts: dict[str, PromptPair], references: dict[str, dict[str, Any]], profiles: dict[str, tuple[str, dict[str, Any]]]) -> None:
        parser = self._parser
        used = {(item.backend, item.pipeline) for item in selections}
        for selector in defaults:
            if selector not in used:
                parser.fail("defaults", f"declares the unused selector {selector[0]}/{selector[1]}")
        chosen = {
            "prompt_pairs": ({item.prompt for item in selections}, prompts),
            "settings_profiles": ({item.profile for item in selections}, profiles),
            "reference_pairs": ({item.knobs["reference_pair"] for item in selections if "reference_pair" in item.knobs}, references),
        }
        for catalog, (names, declared) in chosen.items():
            unused = sorted(set(declared) - names)
            if unused:
                parser.fail(f"{catalog}.{unused[0]}", "is declared but no run selects it")
        self._check_profile_scope(selections, profiles)

    def _check_profile_scope(self, selections: list[_Selection], profiles: dict[str, tuple[str, dict[str, Any]]]) -> None:
        """A profile may serve several pipelines; a setting that only some of them use needs one of those to select it."""
        parser = self._parser
        for name, (backend, settings) in profiles.items():
            pipelines = {item.pipeline for item in selections if item.profile == name}
            field = f"settings_profiles.{name}.settings"
            if backend == H3_BACKEND:
                color = settings.get("color")
                if isinstance(color, dict) and color.get("anchor", "none") != "none" and "i2av" not in pipelines:
                    parser.fail(f"{field}.color.anchor", "requires at least one i2av run that selects this profile; r2av runs are measured, never corrected")
            elif pipelines == {"i2i"}:
                if "color" in settings:
                    parser.fail(f"{field}.color", "applies only to video runs, and no video run selects this profile")
                output = settings.get("output")
                if isinstance(output, dict) and "video_format" in output:
                    parser.fail(f"{field}.output.video_format", "applies only to video runs, and no video run selects this profile")

    def _root_options(self, data: dict[str, Any], name: str) -> _RootOptions:
        output = data.get("output", {})
        if isinstance(output, dict):
            for key, message in REMOVED_OUTPUT_KEYS.items():
                if key in output:
                    self._parser.fail(f"output.{key}", message)
        output_directory = self._parser.output_directory(output, name, {"directory"}).resolve()
        cooldown, source = self._parser.cooldown_policy(data)
        return _RootOptions(output_directory, cooldown, source)

    def _input(self, value: Any, first_pipeline: str) -> Path | None:
        parser = self._parser
        if first_pipeline not in VISUAL_INPUT_PIPELINES:
            if value is not None:
                parser.fail("input", f"is forbidden when the first run uses {first_pipeline}, which takes no image")
            return None
        if not isinstance(value, str) or not value.strip():
            parser.fail("input", f"is required when the first run uses {first_pipeline}")
        path = parser.confined_path(value.strip(), parser.global_config.input_directory, "input")
        if not path.is_file() and not parser.allow_missing_input:
            parser.fail("input", f"file does not exist: {path}")
        return path

    # --- one run's view -------------------------------------------------------------------------------------------

    def _resolve_runs(self, selections: list[_Selection], name: str, catalogs: _Catalogs, input_path: Path | None, output_directory: Path, source_text: str) -> list[EffectiveRun]:
        effective: list[EffectiveRun] = []
        for selection in selections:
            producer = effective[-1].backend_job if effective else None
            consumes_handoff = selection.number > 1 and selection.pipeline in VISUAL_INPUT_PIPELINES
            effective.append(self._compile_run(selection, name, catalogs.prompts[selection.prompt], catalogs.profiles, catalogs.references, input_path, output_directory, producer, consumes_handoff, source_text))
        return effective

    def _compile_run(self, selection: _Selection, name: str, pair: PromptPair, profiles: dict[str, tuple[str, dict[str, Any]]], references: dict[str, dict[str, Any]], input_path: Path | None, output_directory: Path, producer: JobDefinition | None, consumes_handoff: bool, source_text: str) -> EffectiveRun:
        _backend, settings = profiles[selection.profile]
        context = _Context(selection.profile, f"runs[{selection.number - 1}]", selection.knobs.get("reference_pair"))
        handoff_size = self._static_size(producer) if consumes_handoff else None
        if consumes_handoff:
            assert producer is not None
            run_input = self._parser.global_config.input_directory / f"handoff-from-run-{selection.number - 1}"
        else:
            run_input = input_path if selection.pipeline in VISUAL_INPUT_PIPELINES else None
        builder = self._h3_view if selection.backend == H3_BACKEND else self._draw_things_view
        key = (selection.backend, selection.pipeline, selection.prompt, selection.profile, tuple(sorted(selection.knobs.items())), run_input, handoff_size)
        knobs, sources = dict(selection.knobs), dict(selection.sources)
        view = self._views.get(key)
        if view is None:
            view = builder(selection, name, pair, settings, knobs, sources, references, run_input, handoff_size, output_directory, source_text, context)
            self._views[key] = view
        else:
            self._derive_temporal(selection, settings, knobs, sources, context)
        safe = self._safe_settings(selection, view, knobs, sources)
        return EffectiveRun(selection.number, selection.backend, selection.pipeline, selection.prompt, selection.profile, pair, knobs, sources, safe, view)

    def _derive_temporal(self, selection: _Selection, settings: dict[str, Any], knobs: dict[str, Any], sources: dict[str, str], context: _Context) -> None:
        """Add the knobs a run's duration derives, to ``knobs`` and ``sources``, and return nothing."""
        if selection.backend == H3_BACKEND:
            from framecontrol.jobs.backends.h3 import H3_FPS, effective_frames

            seconds = float(knobs["seconds"])
            requested = math.floor(seconds * H3_FPS + 0.5)
            effective = effective_frames(requested)
            knobs.update(requested_frames=requested, effective_frames=effective, effective_seconds=effective / H3_FPS)
            sources.update(requested_frames="derived", effective_frames="backend", effective_seconds="derived")
            return
        if selection.pipeline not in ("i2v", "t2v"):
            return
        field = context.run_field
        if "frames" in knobs:
            return
        base = self._parser.base_config_for(context.profile, str(settings["config_file"]))
        if "seconds" in knobs:
            model = settings.get("config_override", {}).get("model") if isinstance(settings.get("config_override"), dict) else None
            model = model or base.get("model")
            rate = self._frame_rate(model)
            if rate is None:
                self._parser.fail(f"{field}.knobs.seconds", f"cannot be resolved: no authoritative frame rate is known for the Draw Things model {model!r}; set knobs.frames to an exact frame count")
            fps, fps_source = rate
            frames = math.floor(float(knobs["seconds"]) * fps + 0.5)
            if frames < 1:
                self._parser.fail(f"{field}.knobs.seconds", f"is under one frame at {fps:g} fps")
            knobs.update(frames=frames, nominal_fps=fps)
            sources.update(frames="derived", nominal_fps=fps_source)
            return
        frames = base.get("numFrames")
        if is_int(frames) and frames >= 1:
            knobs["frames"], sources["frames"] = frames, "settings_profile"
        # Else no count is stated anywhere: as in version 1, draw-things-cli applies its own, the run records no
        # frames knob, and its measured output_frames says what it made.

    def _frame_rate(self, model: Any) -> tuple[float, str] | None:
        if not isinstance(model, str) or not model:
            return None
        lookup = getattr(self._parser.backends.get(DRAW_THINGS_BACKEND), "frame_rate", None)
        found = lookup(model) if lookup is not None else None
        return (float(found.fps), found.source) if found is not None else None

    def _h3_view(self, selection: _Selection, name: str, pair: PromptPair, settings: dict[str, Any], knobs: dict[str, Any], sources: dict[str, str], references: dict[str, dict[str, Any]], run_input: Path | None, handoff_size: tuple[int, int] | None, output_directory: Path, source_text: str, context: _Context) -> JobDefinition:
        self._derive_temporal(selection, settings, knobs, sources, context)
        profile = dict(settings)
        color = profile.pop("color", None)
        output = profile.pop("output", {})
        field = f"settings_profiles.{context.profile}.settings"
        if not isinstance(output, dict):
            self._parser.fail(f"{field}.output", "must be a mapping")
        self._parser.check_keys(output, {"extension"}, f"{field}.output.")
        if output.get("extension", "mp4") != "mp4":
            self._parser.fail(f"{field}.output.extension", "must be mp4 for h3-metal-cli")
        profile.update(frames=knobs["requested_frames"], seconds=float(knobs["seconds"]), seed=knobs.get("seed", 0))
        mapping: dict[str, Any] = {
            "version": 1,
            "name": name,
            "mode": "h3av" if selection.pipeline == "r2av" else "i2av",
            "input": str(run_input) if run_input is not None else None,
            "run_count": 1,
            "prompt_pairs": [self._pair_data(pair)],
            "run_timeout_seconds": knobs["timeout_seconds"],
            "backend": H3_BACKEND,
            "backend_settings": profile,
            "output": {"directory": str(output_directory), "extension": "mp4"},
        }
        if color is not None:
            mapping["color"] = color
        if selection.pipeline == "r2av":
            chosen = str(knobs["reference_pair"])
            mapping["h3_runs"] = [{"run": 1, "pipeline": "r2av", "reference_pair": chosen}]
            mapping["reference_pairs"] = [{"name": chosen, **references[chosen]}]
        view = self._parse_view(H3_BACKEND, mapping, source_text, context, handoff_size=handoff_size)
        return replace(view, requested_seed=knobs.get("seed"), requested_seed_source="run" if "seed" in knobs else "execution_seed")

    def _draw_things_view(self, selection: _Selection, name: str, pair: PromptPair, settings: dict[str, Any], knobs: dict[str, Any], sources: dict[str, str], references: dict[str, dict[str, Any]], run_input: Path | None, handoff_size: tuple[int, int] | None, output_directory: Path, source_text: str, context: _Context) -> JobDefinition:
        field = f"settings_profiles.{context.profile}.settings"
        override = settings.get("config_override", {})
        if not isinstance(override, dict):
            self._parser.fail(f"{field}.config_override", "must be a mapping")
        override = dict(override)
        self._derive_temporal(selection, settings, knobs, sources, context)
        if "frames" in knobs:
            override["frame_count"] = knobs["frames"]
        if "seed" in knobs:
            override["seed"] = knobs["seed"]
        output = settings.get("output", {})
        if not isinstance(output, dict):
            self._parser.fail(f"{field}.output", "must be a mapping")
        self._parser.check_keys(output, {"extension", "video_format"}, f"{field}.output.")
        video = selection.pipeline != "i2i"
        if not video:
            # A profile shared with video runs keeps its video-only keys for them; a still image has none.
            output = {key: value for key, value in output.items() if key != "video_format"}
        mapping: dict[str, Any] = {
            "version": 1,
            "name": name,
            "mode": selection.pipeline,
            "input": str(run_input) if run_input is not None else None,
            "run_count": 1,
            "prompt_pairs": [self._pair_data(pair)],
            "run_timeout_seconds": knobs["timeout_seconds"],
            "config_file": settings["config_file"],
            "config_override": override,
            "output": {"directory": str(output_directory), **output},
        }
        if "color" in settings and video:
            mapping["color"] = settings["color"]
        resize = settings.get("input_resize")
        if resize is not None:
            if not isinstance(resize, dict):
                self._parser.fail(f"{field}.input_resize", "must be a mapping")
            self._parser.check_keys(resize, set(INPUT_RESIZE_KEYS), f"{field}.input_resize.")
            for key, legacy in INPUT_RESIZE_KEYS.items():
                if key in resize:
                    mapping[legacy] = resize[key]
        view = self._parse_view(DRAW_THINGS_BACKEND, mapping, source_text, context, profile=context.profile, handoff_size=handoff_size)
        # A version-2 run takes its seed only from its knob or the execution; the base configuration's seed is not used.
        from framecontrol.jobs.backends.draw_things import DrawThingsJobSettings

        base_config = {key: value for key, value in view.base_config.items() if key != "seed"}
        ignored = dict(view.ignored_config)
        if "seed" in view.base_config:
            ignored["seed"] = view.base_config["seed"]
        data = replace(view.backend_data, base_config=base_config) if isinstance(view.backend_data, DrawThingsJobSettings) else view.backend_data
        return replace(view, base_config=base_config, backend_data=data, ignored_config=ignored, backend=DRAW_THINGS_BACKEND, requested_seed=knobs.get("seed"), requested_seed_source="run" if "seed" in knobs else "execution_seed")

    @staticmethod
    def _pair_data(pair: PromptPair) -> dict[str, Any]:
        data: dict[str, Any] = {"name": pair.name, "positive": pair.positive, "default": True}
        if pair.negative is not None:
            data["negative"] = pair.negative
        return data

    def _parse_view(self, backend: str, mapping: dict[str, Any], source_text: str, context: _Context, *, profile: str | None = None, handoff_size: tuple[int, int] | None) -> JobDefinition:
        """Let the run's own adapter validate the one-run job; its errors name the version-2 field they come from."""
        nested = self._parser.nested(profile=profile, handoff_size=handoff_size)
        try:
            return self._parser.backends.get(backend).parse_job(nested, mapping, source_text)
        except InputError as error:
            message, field = str(error), error.field
            if field is not None:
                field = self._remap(field, context)
                message = message.replace(f"'{error.field}'", f"'{field}'", 1)
            # Advice in a message can name a version-1 key the file no longer has; say where it lives now.
            resize = f"settings_profiles.{context.profile}.settings.input_resize"
            for old, new in (("max_input_crop_percent", f"{resize}.max_crop_percent"), ("desired_input_width", f"{resize}.desired_width"), ("desired_input_height", f"{resize}.desired_height")):
                if old in message:
                    message = message.replace(old, new)
                    field = field or new
            raise InputError(message, field=field, path=error.path) from error

    @staticmethod
    def _remap(field: str, context: _Context) -> str:
        profile = f"settings_profiles.{context.profile}.settings"
        rules = (
            ("backend_settings", profile),
            ("config_file", f"{profile}.config_file"),
            ("config_override", f"{profile}.config_override"),
            ("color", f"{profile}.color"),
            ("output", f"{profile}.output"),
            ("run_timeout_seconds", f"{context.run_field}.knobs.timeout_seconds"),
            ("desired_input_width", f"{profile}.input_resize.desired_width"),
            ("desired_input_height", f"{profile}.input_resize.desired_height"),
            ("max_input_crop_percent", f"{profile}.input_resize.max_crop_percent"),
            ("h3_runs[0].reference_pair", f"{context.run_field}.knobs.reference_pair"),
            ("h3_runs", context.run_field),
            ("reference_pairs[0]", f"reference_pairs.{context.reference}"),
        )
        for old, new in rules:
            if field == old or field.startswith((f"{old}.", f"{old}[")):
                return new + field[len(old) :]
        return field

    def _safe_settings(self, selection: _Selection, view: JobDefinition, knobs: dict[str, Any], sources: dict[str, str]) -> dict[str, Any]:
        safe = dict(self._parser.backends.get(selection.backend).safe_settings(view))
        safe["settings_profile"] = selection.profile
        safe.pop("h3_runs", None)
        if "seed" in knobs:
            safe["seed"], safe["seed_source"] = knobs["seed"], sources["seed"]
        else:
            safe["seed"], safe["seed_source"] = None, "execution_seed"
        if selection.backend == H3_BACKEND:
            for stale in ("requested_frames", "requested_seconds", "effective_frames", "effective_seconds"):
                safe.pop(stale, None)
        else:
            safe["color"] = view.color.as_dict()
            if "frames" in knobs:
                safe["frames"] = knobs["frames"]
        return safe

    # --- cross-run facts ------------------------------------------------------------------------------------------

    def _check_edges(self, runs: list[EffectiveRun]) -> None:
        for index, run in enumerate(runs[1:], start=1):
            if run.pipeline in VISUAL_INPUT_PIPELINES:
                self._check_edge(run, runs[index - 1].backend_job)

    def _static_size(self, view: JobDefinition | None) -> tuple[int, int] | None:
        """The size of what a run's output hands off, known from its profile before anything runs."""
        if view is None:
            return None
        lookup = getattr(self._parser.backends.get(view.backend), "output_size", None)
        return lookup(view) if lookup is not None else None

    def _check_edge(self, run: EffectiveRun, producer: JobDefinition | None) -> None:
        """A handoff into a Draw Things run with no ``input_resize`` must already be the size the profile generates."""
        view = run.backend_job
        if view.backend == H3_BACKEND or view.input_resize is not None:
            return
        have, need = self._static_size(producer), self._static_size(view)
        if have is not None and need is not None and have != need:
            field = f"runs[{run.number - 1}].settings_profile"
            self._parser.fail(field, f"generates {need[0]}x{need[1]}, but run {run.number - 1} hands off {have[0]}x{have[1]}; set 'input_resize' in settings profile {run.settings_profile} or use matching sizes")

    # --- the job --------------------------------------------------------------------------------------------------

    def _root(self, name: str, effective: list[EffectiveRun], input_path: Path | None, prompts: dict[str, PromptPair], output_directory: Path, cooldown: CooldownPolicy, cooldown_source: str, source_text: str) -> JobDefinition:
        parser = self._parser
        first = effective[0].backend_job
        backends = {run.backend for run in effective}
        models = {run.backend_job.model for run in effective}
        seeds = {run.seed for run in effective}
        shared_seed = next(iter(seeds)) if len(seeds) == 1 and None not in seeds else None
        timeouts = [run.timeout_seconds or 0.0 for run in effective]
        plan = [run.public_plan() for run in effective]
        return replace(
            first,
            path=parser.path,
            name=name,
            input=input_path,
            run_count=len(effective),
            prompt_pairs=tuple(prompts.values()),
            output_directory=output_directory,
            extension=first.extension,
            config_file="",
            base_config={},
            config_override=ConfigOverride(),
            model=next(iter(models)) if len(models) == 1 else "",
            run_timeout_seconds=max(timeouts) or None,
            requested_seed=shared_seed,
            requested_seed_source="run" if shared_seed is not None else "random",
            backend=next(iter(backends)) if len(backends) == 1 else "mixed",
            backend_settings={"format_version": 2, "runs": plan},
            backend_data=None,
            video_format=None,
            ignored_config={},
            size=None,
            ignored_size=(),
            color=ColorPolicy(),
            cooldown=cooldown,
            cooldown_source=cooldown_source,
            source_text=source_text,
            version=2,
            effective_runs=tuple(effective),
        )
