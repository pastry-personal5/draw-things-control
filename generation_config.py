"""Find base Draw Things configurations and apply job overrides to them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from configuration import load_config

DT_CONFIG_DIRECTORY = Path(__file__).resolve().parent / "dt-config"

# Job override keys that draw-things-cli has no flag for, and their Draw Things names.
CONFIG_ONLY_KEYS = {"refiner_model": "refinerModel", "refiner_start": "refinerStart", "shift": "shift"}


def find_config_file(name: str, directory: Path = DT_CONFIG_DIRECTORY) -> Path:
    """Resolve a bare configuration file name inside the dt-config directory."""
    if not name or "/" in name or "\\" in name or name in {".", ".."} or ".." in Path(name).parts:
        raise ValueError(f"'config_file' must be a file name in {directory}, not a path: {name}")
    path = directory / name
    if not path.is_file():
        available = sorted(candidate.name for candidate in directory.glob("*.json"))
        listing = ", ".join(available) if available else "none"
        raise ValueError(f"'config_file' {name} is not in {directory} (available: {listing})")
    return path


def load_base_config(name: str, directory: Path = DT_CONFIG_DIRECTORY) -> dict[str, Any]:
    """Load the named base configuration as a JSON object."""
    return load_config(find_config_file(name, directory))


def build_config_json(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Apply the overrides that have no command-line flag onto the base configuration."""
    config = dict(base)
    for key, config_key in CONFIG_ONLY_KEYS.items():
        if override.get(key) is not None:
            config[config_key] = override[key]
    return config
