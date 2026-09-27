"""Validate a job's ``config_override``."""

from __future__ import annotations

from typing import Any

from draw_things_control.core.numbers import is_int, is_number
from draw_things_control.jobs.definition import OVERRIDE_KEYS, ConfigOverride, GenerationMode
from draw_things_control.jobs.prompt_pairs import CheckKeys, Fail

# draw-things-cli takes a UInt32 seed.
MAX_SEED = 2**32 - 1


def parse_config_override(value: Any, mode: GenerationMode, fail: Fail, check_keys: CheckKeys) -> ConfigOverride:
    if not isinstance(value, dict):
        fail("config_override", "must be a mapping")
    check_keys(value, OVERRIDE_KEYS, "config_override.")

    def field(key: str) -> str:
        return f"config_override.{key}"

    for key in ("model", "refiner_model"):
        if key in value and (not isinstance(value[key], str) or not value[key]):
            fail(field(key), "must be a non-empty model name")
    for key, low, high in (("refiner_start", 0, 1), ("strength", 0, 1)):
        if key in value and (not is_number(value[key]) or not low <= value[key] <= high):
            fail(field(key), f"must be a number from {low} to {high}")
    for key, minimum in (("steps", 1), ("frame_count", 1)):
        if key in value and (not is_int(value[key]) or value[key] < minimum):
            fail(field(key), f"must be an integer >= {minimum}")
    if "seed" in value and (not is_int(value["seed"]) or not 0 <= value["seed"] <= MAX_SEED):
        fail(field("seed"), f"must be an integer from 0 to {MAX_SEED}")
    if "guidance_scale" in value and (not is_number(value["guidance_scale"]) or value["guidance_scale"] < 0):
        fail(field("guidance_scale"), "must be a number >= 0")
    if "shift" in value and (not is_number(value["shift"]) or value["shift"] <= 0):
        fail(field("shift"), "must be a number > 0")
    for key in ("width", "height"):
        if key in value and (not is_int(value[key]) or value[key] <= 0 or value[key] % 64):
            fail(field(key), "must be a positive multiple of 64")
    if "frame_count" in value and not mode.is_video:
        fail(field("frame_count"), "is not allowed in i2i jobs")
    return ConfigOverride(**value)
