"""Find base Draw Things configurations and apply job overrides to them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from draw_things_control.core.configuration import YAML_SUFFIXES, is_yaml_file, load_config

# The Draw Things configurations a job names in config_file: data/params/ in the repository.
PARAMS_DIRECTORY = Path(__file__).resolve().parents[3] / "data" / "params"
# The job files: the TUI's default data directory, and the only one whose files get job IDs (J0001).
JOBS_DIRECTORY = Path(__file__).resolve().parents[3] / "data" / "jobs"

# Job override keys that draw-things-cli has no flag for, and their Draw Things names.
CONFIG_ONLY_KEYS = {"refiner_model": "refinerModel", "refiner_start": "refinerStart", "shift": "shift"}


def find_config_file(name: str, directory: Path = PARAMS_DIRECTORY) -> Path:
    """Resolve a bare configuration file name inside the params directory (data/params/)."""
    if not name or "/" in name or "\\" in name or name in {".", ".."} or ".." in Path(name).parts:
        raise ValueError(f"'config_file' must be a file name in {directory}, not a path: {name}")
    if Path(name).suffix.lower() == ".json":
        raise ValueError(_json_config_message(name, directory))
    if not is_yaml_file(name):
        raise ValueError(f"'config_file' {name} must name a YAML file (.yaml or .yml) in {directory}")
    path = directory / name
    if not path.is_file():
        available = sorted(candidate.name for candidate in directory.iterdir() if candidate.is_file() and is_yaml_file(candidate)) if directory.is_dir() else []
        listing = ", ".join(available) if available else "none"
        raise ValueError(f"'config_file' {name} is not in {directory} (available: {listing})")
    return path


def _json_config_message(name: str, directory: Path) -> str:
    """Explain that jobs need YAML, naming the YAML file with the same stem when there is one."""
    stem = Path(name).stem
    for suffix in sorted(YAML_SUFFIXES):
        if (directory / f"{stem}{suffix}").is_file():
            return f"'config_file' {name} is JSON; name {stem}{suffix} instead"
    return f"'config_file' {name} is JSON, but a job needs a YAML configuration; write {stem}.yaml in {directory} (the JSON file is left as it is)"


def load_base_config(name: str, directory: Path = PARAMS_DIRECTORY) -> dict[str, Any]:
    """Load the named YAML base configuration as an object JSON can hold."""
    return load_config(find_config_file(name, directory))


def build_config_json(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Apply the overrides that have no command-line flag onto the base configuration."""
    config = dict(base)
    for key, config_key in CONFIG_ONLY_KEYS.items():
        if override.get(key) is not None:
            config[config_key] = override[key]
    return config
