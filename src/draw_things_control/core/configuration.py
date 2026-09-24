"""Read Draw Things JSON configuration overrides."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_config(path: Path) -> dict[str, Any]:
    """Load a JSON object from a configuration file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"Cannot read configuration file {path}: {error.strerror}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"Configuration is not valid JSON: {path} ({error.msg} on line {error.lineno})") from error
    if not isinstance(data, dict):
        raise ValueError("Configuration must contain a JSON object")
    return data
