"""What a run's timing is measured in: the executable's build and the host's load. No path is kept."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# The 1, 5, and 15 minute load averages.
HostLoad = tuple[float, float, float]


def host_load() -> HostLoad | None:
    """The host's 1, 5, and 15 minute load averages, or None where it gives none."""
    try:
        return os.getloadavg()
    except OSError:
        return None


def executable_facts(executable: str | os.PathLike[str] | None) -> dict[str, Any]:
    """The executable's size and modification time, standing in for a fingerprint: enough to notice a changed build,
    not to authenticate one. Nothing for an executable that cannot be read."""
    if executable is None:
        return {}
    try:
        status = Path(executable).stat()
    except OSError:
        return {}
    return {"executable_bytes": status.st_size, "executable_mtime": status.st_mtime}
