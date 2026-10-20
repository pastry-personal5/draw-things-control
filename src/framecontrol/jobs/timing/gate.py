"""Pure M15 held-out partitioning and deterministic paired replay gate.

The updater supplies one paired error observation per execution. Keeping the
statistical decision here makes it impossible for a
candidate to accidentally partition its own training evidence differently
from the baseline it is compared with.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

MARGIN = 0.01
BOOTSTRAP_SAMPLES = 10_000


@dataclass(frozen=True)
class Partition:
    training: tuple[Mapping[str, int], ...]
    holdout: tuple[Mapping[str, int], ...]
    reason: str | None = None


@dataclass(frozen=True)
class GateResult:
    verdict: str
    interval: tuple[float, float] | None
    differences: tuple[float, ...]
    reason: str


def partition(entries: Iterable[Mapping[str, int]], checkpoint_execution: int | None) -> Partition:
    """Split entries by execution, reserving the newest post-checkpoint ones.

    Multiple runs in an execution stay together.  The returned entries retain
    chronological order and no holdout item can be used by a caller's fit.
    """
    if checkpoint_execution is None:
        return Partition((), (), "insufficient_evidence")
    ordered = sorted(entries, key=lambda item: (item["e"], item.get("r", 0)))
    executions = sorted({item["e"] for item in ordered})
    eligible = [execution for execution in executions if execution > checkpoint_execution]
    hold_count = min(8, max(3, math.ceil(len(executions) * 0.2)))
    if len(eligible) < hold_count:
        return Partition((), (), "insufficient_evidence")
    hold_ids = frozenset(eligible[-hold_count:])
    holdout = tuple(item for item in ordered if item["e"] in hold_ids)
    training = tuple(item for item in ordered if item["e"] not in hold_ids)
    if len({item["e"] for item in training}) < 3:
        return Partition(training, holdout, "insufficient_evidence")
    return Partition(training, holdout)


def paired_bootstrap(differences: Sequence[float], execution_ids: Sequence[int], *, samples: int = BOOTSTRAP_SAMPLES) -> tuple[float, float]:
    """A deterministic 90% paired-bootstrap interval for median differences."""
    if len(differences) != len(execution_ids) or not differences or samples < 1 or len(set(execution_ids)) != len(execution_ids) or not all(math.isfinite(value) for value in differences):
        raise ValueError("paired differences must be finite and have one value per distinct execution, with at least one sample")
    pairs = sorted(zip(execution_ids, differences, strict=True))
    seed_bytes = hashlib.sha256(",".join(str(execution) for execution, _difference in pairs).encode()).digest()
    generator = np.random.default_rng(int.from_bytes(seed_bytes[:8], "big"))
    values = np.asarray([difference for _execution, difference in pairs], dtype=float)
    draws = generator.integers(0, len(values), size=(samples, len(values)))
    medians = np.median(values[draws], axis=1)
    return (float(np.quantile(medians, 0.05)), float(np.quantile(medians, 0.95)))


def decide(differences: Sequence[float], execution_ids: Sequence[int], *, valid: bool = True, margin: float = MARGIN) -> GateResult:
    """Return promote/reject/inconclusive for candidate-minus-baseline error.

    Negative values improve error.  Small holdouts may reject or promote only
    for a unanimous, material sign result; otherwise they are intentionally
    inconclusive rather than an invitation to repeatedly look at noise.
    """
    if not valid:
        return GateResult("reject", None, tuple(differences), "invalid_candidate")
    if len(differences) != len(execution_ids) or len(differences) < 3 or not all(math.isfinite(value) for value in differences):
        return GateResult("inconclusive", None, tuple(differences), "insufficient_evidence")
    interval = paired_bootstrap(differences, execution_ids)
    if len(differences) < 5:
        if all(value <= -margin for value in differences):
            return GateResult("promote", interval, tuple(differences), "unanimous_material_improvement")
        if all(value > margin for value in differences):
            return GateResult("reject", interval, tuple(differences), "unanimous_material_regression")
        return GateResult("inconclusive", interval, tuple(differences), "fewer_than_five_holdouts")
    if interval[0] > 0:
        return GateResult("reject", interval, tuple(differences), "clear_regression")
    if interval[1] <= margin and float(np.median(differences)) < 0:
        return GateResult("promote", interval, tuple(differences), "no_worse_than_margin")
    return GateResult("inconclusive", interval, tuple(differences), "uncertain")


__all__ = ["BOOTSTRAP_SAMPLES", "GateResult", "MARGIN", "Partition", "decide", "paired_bootstrap", "partition"]
