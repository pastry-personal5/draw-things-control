"""The cooldown between the runs of a job: its mapping in a job or the global configuration, and the wait it gives."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from draw_things_control.core.numbers import is_number, number_text
from draw_things_control.core.paths import DEFAULT_PATHS

MAX_COOLDOWN_SECONDS = 3600
COOLDOWN_ERROR = f"must be a number of seconds from 0 to {MAX_COOLDOWN_SECONDS}"
DEFAULT_COOLDOWN_RATIO = 0.5
# The keys each cooldown mode takes, besides mode itself.
COOLDOWN_MODE_KEYS = {"auto": ("ratio", "minimum_seconds", "maximum_seconds"), "manual": ("seconds",), "off": ()}
COOLDOWN_KEYS = {"mode", *(key for keys in COOLDOWN_MODE_KEYS.values() for key in keys)}


@dataclass(frozen=True)
class CooldownWait:
    """The wait after one run: its seconds, and which bound set them (``minimum``, ``maximum``, or None)."""

    seconds: float
    bound: str | None = None


@dataclass(frozen=True)
class CooldownPolicy:
    """How long a job waits after each successful run but the last.

    ``auto`` waits ``ratio`` of the run's time, rounded up to a whole second and kept between the bounds;
    ``manual`` waits a fixed ``seconds``; ``off`` never waits.
    """

    mode: str
    ratio: float = DEFAULT_COOLDOWN_RATIO
    seconds: float = 0.0
    minimum_seconds: float = 0.0
    maximum_seconds: float = float(MAX_COOLDOWN_SECONDS)

    def wait_after(self, run_seconds: float) -> CooldownWait:
        """The wait after a run that took ``run_seconds``."""
        if self.mode == "manual":
            return CooldownWait(self.seconds)
        if self.mode == "off":
            return CooldownWait(0.0)
        # Decimal, so a share that is a whole number in decimal is not rounded up by a binary remainder (1200 x 0.1 is 120, not 121).
        share = math.ceil(Decimal(repr(float(run_seconds))) * Decimal(repr(float(self.ratio))))
        if share < self.minimum_seconds:
            return CooldownWait(self.minimum_seconds, "minimum")
        if share > self.maximum_seconds:
            return CooldownWait(self.maximum_seconds, "maximum")
        return CooldownWait(float(share))

    @property
    def fixed_seconds(self) -> float | None:
        """The wait as the old single number: manual's seconds, 0 for off, None for auto."""
        return {"manual": self.seconds, "off": 0.0}.get(self.mode)

    def as_dict(self) -> dict[str, Any]:
        """The mapping as resolved, defaults filled in, as a job or global configuration would write it."""
        return {"mode": self.mode, **{key: getattr(self, key) for key in COOLDOWN_MODE_KEYS[self.mode]}}


DEFAULT_COOLDOWN = CooldownPolicy(mode="auto")


def is_cooldown(value: Any) -> bool:
    """Whether ``value`` is a valid cooldown: a number of seconds from 0 to MAX_COOLDOWN_SECONDS."""
    # The chained comparison also rejects NaN, and infinity is over the limit.
    return is_number(value) and 0 <= value <= MAX_COOLDOWN_SECONDS


def parse_cooldown(value: Any, key: str) -> CooldownPolicy:
    """Validate a ``cooldown`` mapping from a job or the global configuration; errors name ``key`` and the bad entry."""
    if not isinstance(value, dict):
        raise ValueError(f"'{key}' must be a mapping with a mode: auto, manual, or off")
    if "mode" not in value:
        raise ValueError(f"'{key}.mode' is required: auto, manual, or off")
    mode = value["mode"]
    # YAML reads an unquoted off as false.
    if mode is False:
        mode = "off"
    if not isinstance(mode, str) or mode not in COOLDOWN_MODE_KEYS:
        raise ValueError(f"'{key}.mode' must be auto, manual, or off")
    allowed = COOLDOWN_MODE_KEYS[mode]
    for name in value:
        if name not in COOLDOWN_KEYS:
            raise ValueError(f"'{key}.{name}' is not a known key")
        if name != "mode" and name not in allowed:
            raise ValueError(f"'{key}.{name}' is not used with mode {mode}")
    if mode == "manual" and "seconds" not in value:
        raise ValueError(f"'{key}.seconds' is required with mode manual")
    for name in ("seconds", "minimum_seconds", "maximum_seconds"):
        if name in value and not is_cooldown(value[name]):
            raise ValueError(f"'{key}.{name}' {COOLDOWN_ERROR}")
    ratio = value.get("ratio", DEFAULT_COOLDOWN_RATIO)
    if not is_number(ratio) or not 0 < ratio <= 1:
        raise ValueError(f"'{key}.ratio' must be a number above 0 and up to 1")
    policy = CooldownPolicy(
        mode=mode,
        ratio=float(ratio),
        seconds=float(value.get("seconds", 0.0)),
        minimum_seconds=float(value.get("minimum_seconds", 0.0)),
        maximum_seconds=float(value.get("maximum_seconds", MAX_COOLDOWN_SECONDS)),
    )
    if policy.minimum_seconds > policy.maximum_seconds:
        raise ValueError(f"'{key}.minimum_seconds' must not be above '{key}.maximum_seconds' ({number_text(policy.maximum_seconds)})")
    return policy


def replaced_cooldown_message(value: Any) -> str:
    """The error for the old ``cooldown_seconds`` key, with the mapping that keeps the same wait."""
    if is_cooldown(value) and value == 0:
        same = "{mode: off}"
    else:
        same = f"{{mode: manual, seconds: {number_text(value) if is_cooldown(value) else 900}}}"
    return f"'cooldown_seconds' was replaced by 'cooldown'; write cooldown: {same} for the same wait, or use mode auto or off (see {DEFAULT_PATHS.example_global_config.relative_to(DEFAULT_PATHS.root)})"
