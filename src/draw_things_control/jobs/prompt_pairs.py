"""Validate a job's ``prompt_pairs`` and which pair each run uses."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, NoReturn

from draw_things_control.core.numbers import is_int
from draw_things_control.jobs.definition import PromptPair

NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")
PAIR_KEYS = {"name", "positive", "negative", "runs", "default"}

# Raises the job's InputError for a field and a problem.
Fail = Callable[[str, str], NoReturn]
# Refuses a key of a mapping that is not in the allowed set, naming it with the prefix.
CheckKeys = Callable[[dict[str, Any], set[str], str], None]


def parse_prompt_pairs(value: Any, run_count: int, fail: Fail, check_keys: CheckKeys) -> tuple[PromptPair, ...]:
    if not isinstance(value, list) or not value:
        fail("prompt_pairs", "must be a list with at least one pair")
    pairs: list[PromptPair] = []
    for index, item in enumerate(value):
        pairs.append(_prompt_pair(index, item, {pair.name for pair in pairs}, fail, check_keys))
    return _assign_runs(pairs, run_count, fail)


def _prompt_pair(index: int, item: Any, taken_names: set[str], fail: Fail, check_keys: CheckKeys) -> PromptPair:
    field = f"prompt_pairs[{index}]"
    if not isinstance(item, dict):
        fail(field, "must be a mapping")
    check_keys(item, PAIR_KEYS, f"{field}.")
    name = item.get("name")
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        fail(f"{field}.name", "must be lowercase letters, digits, and hyphens")
    if name in taken_names:
        fail(f"{field}.name", f"duplicates the pair name '{name}'")
    positive = item.get("positive")
    if not isinstance(positive, str) or not positive.strip():
        fail(f"{field}.positive", "is required and must be non-empty text")
    negative = item.get("negative")
    if negative is not None and not isinstance(negative, str):
        fail(f"{field}.negative", "must be text")
    runs = item.get("runs", [])
    if not isinstance(runs, list) or not all(is_int(number) for number in runs):
        fail(f"{field}.runs", "must be a list of run numbers")
    default = item.get("default", False)
    if not isinstance(default, bool):
        fail(f"{field}.default", "must be true or false")
    return PromptPair(name=name, positive=positive, negative=negative, runs=tuple(runs), default=default)


def _assign_runs(pairs: list[PromptPair], run_count: int, fail: Fail) -> tuple[PromptPair, ...]:
    """Check which pair each run uses; a single pair with no default is the default."""
    if sum(pair.default for pair in pairs) > 1:
        fail("prompt_pairs", "may mark at most one pair as default")
    if len(pairs) == 1 and not pairs[0].default:
        only = pairs[0]
        pairs[0] = PromptPair(name=only.name, positive=only.positive, negative=only.negative, runs=only.runs, default=True)
    assigned: dict[int, str] = {}
    for index, pair in enumerate(pairs):
        for number in pair.runs:
            if not 1 <= number <= run_count:
                fail(f"prompt_pairs[{index}].runs", f"lists run {number}, outside 1..{run_count}")
            if number in assigned:
                fail(f"prompt_pairs[{index}].runs", f"lists run {number}, already assigned to pair '{assigned[number]}'")
            assigned[number] = pair.name
    if not any(pair.default for pair in pairs):
        for number in range(1, run_count + 1):
            if number not in assigned:
                fail("prompt_pairs", f"assign run {number} to a pair, or mark one pair 'default: true'")
    return tuple(pairs)
