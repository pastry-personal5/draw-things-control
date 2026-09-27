"""Reading job files and the global configuration, for every front end; nothing here prints or exits."""

from __future__ import annotations

from pathlib import Path

from draw_things_control.core.global_config import GlobalConfig, load_global_config
from draw_things_control.core.paths import ProjectPaths
from draw_things_control.core.yaml_files import is_yaml_file
from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.jobs.parsing import load_job


def read_settings(global_config: Path, paths: ProjectPaths) -> GlobalConfig:
    """Load the global configuration; raises ValueError if it is invalid."""
    path = global_config.expanduser()
    return load_global_config(path, is_default=path == paths.global_config)


def read_job(job_file: Path, global_config: Path | GlobalConfig, paths: ProjectPaths, *, decode_input: bool = True) -> tuple[JobDefinition, GlobalConfig]:
    """Load the job and the global configuration (a path, or one already loaded); raises ValueError if either is invalid."""
    settings = global_config if isinstance(global_config, GlobalConfig) else read_settings(global_config, paths)
    return load_job(job_file, settings, paths.params, decode_input=decode_input), settings


def job_files(directory: Path) -> list[Path]:
    """The ``*.yaml`` and ``*.yml`` files (in any letter case) directly in ``directory``, by file name; dotfiles and sub-directories are skipped.

    Raises OSError if the directory cannot be read.
    """
    return sorted((path for path in directory.iterdir() if is_yaml_file(path) and not path.name.startswith(".") and path.is_file()), key=lambda path: path.name)
