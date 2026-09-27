"""Read Draw Things configurations (YAML only), find base configurations, and apply job overrides to them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from draw_things_control.core.arguments import CONFIG_ONLY_KEYS
from draw_things_control.core.paths import DEFAULT_PATHS
from draw_things_control.core.yaml_files import YAML_SUFFIXES, is_yaml_file, parse_yaml_mapping

# The Draw Things configurations a job names in config_file: data/params/ in the repository.
PARAMS_DIRECTORY = DEFAULT_PATHS.params
# The job files: the TUI's default data directory, and the only one whose files get job IDs (J0001).
JOBS_DIRECTORY = DEFAULT_PATHS.jobs


def find_config_file(name: str, directory: Path = PARAMS_DIRECTORY) -> Path:
    """Resolve a bare configuration file name inside the params directory (data/params/)."""
    if not name or "/" in name or "\\" in name or name in {".", ".."} or ".." in Path(name).parts:
        raise ValueError(f"'config_file' must be a file name in {directory}, not a path: {name}")
    if Path(name).suffix.lower() == ".json":
        raise ValueError(json_config_message("'config_file'", name, directory, "a job"))
    if not is_yaml_file(name):
        raise ValueError(f"'config_file' {name} must name a YAML file (.yaml or .yml) in {directory}")
    path = directory / name
    if not path.is_file():
        available = sorted(candidate.name for candidate in directory.iterdir() if candidate.is_file() and is_yaml_file(candidate)) if directory.is_dir() else []
        listing = ", ".join(available) if available else "none"
        raise ValueError(f"'config_file' {name} is not in {directory} (available: {listing})")
    return path


def json_config_message(subject: str, name: str, directory: Path, needs: str, verb: str = "name") -> str:
    """Explain that ``needs`` takes a YAML configuration, naming the YAML file with the same stem in ``directory`` when there is one."""
    stem = Path(name).stem
    for suffix in sorted(YAML_SUFFIXES):
        if (directory / f"{stem}{suffix}").is_file():
            return f"{subject} {name} is JSON; {verb} {stem}{suffix} instead"
    return f"{subject} {name} is JSON, but {needs} needs a YAML configuration; write {stem}.yaml in {directory} (the JSON file is left as it is)"


def load_config(path: Path) -> dict[str, Any]:
    """Load a Draw Things configuration from a YAML file, as an object JSON can hold; a JSON file is refused, and left as it is."""
    if not is_yaml_file(path):
        if path.suffix.lower() == ".json":
            raise ValueError(json_config_message("Configuration file", str(path), path.parent, "this command", verb="use"))
        raise ValueError(f"Configuration file {path} must be a YAML file (.yaml or .yml)")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"Cannot read configuration file {path}: {error.strerror}") from error
    return parse_yaml_mapping(text, path, "Configuration", require_json=True)


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
