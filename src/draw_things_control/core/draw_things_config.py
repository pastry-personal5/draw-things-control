"""Read Draw Things configurations (YAML only), find base configurations, and apply job overrides to them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from draw_things_control.core.arguments import CONFIG_ONLY_KEYS
from draw_things_control.core.paths import linked_component
from draw_things_control.core.yaml_files import YAML_SUFFIXES, is_yaml_file, read_yaml_file


def find_config_file(name: str, directory: Path) -> Path:
    """Resolve a bare configuration file name inside the params directory (data/params/)."""
    if not name or "/" in name or "\\" in name or name in {".", ".."} or ".." in Path(name).parts:
        raise ValueError(f"'config_file' must be a file name in {directory}, not a path: {name}")
    if linked_component(directory, directory.parent.parent) is not None:
        raise ValueError("'config_file' cannot use a symbolic link in the parameter directory")
    if Path(name).suffix.lower() == ".json":
        raise ValueError(json_config_message("'config_file'", name, directory, "a job"))
    if not is_yaml_file(name):
        raise ValueError(f"'config_file' {name} must name a YAML file (.yaml or .yml) in {directory}")
    path = directory / name
    if path.is_symlink():
        raise ValueError("'config_file' cannot name a symbolic link")
    if not path.is_file():
        available = sorted(candidate.name for candidate in directory.iterdir() if not candidate.is_symlink() and candidate.is_file() and is_yaml_file(candidate)) if directory.is_dir() else []
        listing = ", ".join(available) if available else "none"
        raise ValueError(f"'config_file' {name} is not in {directory} (available: {listing})")
    return path


def json_config_message(subject: str, name: str, directory: Path, needs: str, verb: str = "name") -> str:
    """Explain that ``needs`` takes a YAML configuration, naming the YAML file with the same stem in ``directory`` when there is one."""
    stem = Path(name).stem
    for suffix in sorted(YAML_SUFFIXES):
        if not (directory / f"{stem}{suffix}").is_symlink() and (directory / f"{stem}{suffix}").is_file():
            return f"{subject} {name} is JSON; {verb} {stem}{suffix} instead"
    return f"{subject} {name} is JSON, but {needs} needs a YAML configuration; write {stem}.yaml in {directory} (the JSON file is left as it is)"


def load_config(path: Path, *, max_bytes: int | None = None) -> dict[str, Any]:
    """Load a Draw Things configuration from a YAML file, as an object JSON can hold; a JSON file is refused, and left as it is."""
    if not is_yaml_file(path):
        if path.suffix.lower() == ".json":
            raise ValueError(json_config_message("Configuration file", str(path), path.parent, "this command", verb="use"))
        raise ValueError(f"Configuration file {path} must be a YAML file (.yaml or .yml)")
    data, _text = read_yaml_file(path, "Configuration", require_json=True, max_bytes=max_bytes)
    return data


def load_base_config(name: str, directory: Path, *, max_bytes: int | None = None) -> dict[str, Any]:
    """Load the named YAML base configuration as an object JSON can hold."""
    return load_config(find_config_file(name, directory), max_bytes=max_bytes)


def build_config_json(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Apply the overrides that have no command-line flag onto the base configuration."""
    config = dict(base)
    for key, config_key in CONFIG_ONLY_KEYS.items():
        if override.get(key) is not None:
            config[config_key] = override[key]
    return config
