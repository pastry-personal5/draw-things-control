"""Record what each run of a job did, in a JSON manifest beside its outputs."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RunRecord:
    """One run: its prompts, files, command, and result."""

    batch: int
    pair: str
    positive: str
    negative: str | None
    input: str | None
    output: str | None
    last_frame: str | None
    command: list[str]
    started_at: str
    seconds: float | None = None
    exit_code: int | None = None
    status: str = "running"


@dataclass
class JobManifest:
    """A job's settings and the record of every run so far."""

    job_file: str
    name: str
    mode: str
    config_file: str
    config_override: dict[str, Any]
    seed: int
    seed_source: str
    started_at: str
    log_file: str | None
    finished_at: str | None = None
    status: str = "running"
    runs: list[RunRecord] = field(default_factory=list)


def write_manifest(path: Path, manifest: JobManifest) -> None:
    """Write the manifest atomically, so a crash never leaves a partial file."""
    text = json.dumps(asdict(manifest), indent=2, ensure_ascii=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
