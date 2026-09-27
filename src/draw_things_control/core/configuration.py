"""Read Draw Things configurations from YAML or JSON files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from draw_things_control.core.yaml_files import YAML_SUFFIXES, is_yaml_file, parse_yaml_mapping

__all__ = ["YAML_SUFFIXES", "is_yaml_file", "load_config"]


def load_config(path: Path) -> dict[str, Any]:
    """Load a configuration object from a YAML file (by extension) or a JSON file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"Cannot read configuration file {path}: {error.strerror}") from error
    if is_yaml_file(path):
        return parse_yaml_mapping(text, path, "Configuration", require_json=True)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Configuration is not valid JSON: {path} ({error.msg} on line {error.lineno})") from error
    except RecursionError as error:
        raise ValueError(f"Configuration is nested too deeply: {path}") from error
    if not isinstance(data, dict):
        raise ValueError("Configuration must contain a JSON object")
    return data
