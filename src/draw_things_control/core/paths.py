"""Where the project keeps its files, worked out once."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DATABASE_FILE_NAME = "dtc.db"
LOCK_FILE_NAME = "run.lock"
SERVER_TOKEN_FILE_NAME = "server-token"
# As a person names them in messages: relative to the project root.
GLOBAL_CONFIG_RELATIVE = Path("config/global-config.yaml")
EXAMPLE_GLOBAL_CONFIG_RELATIVE = Path("config/global-config.example.yaml")


@dataclass(frozen=True)
class ProjectPaths:
    """The directories and files of one project, all below its root."""

    root: Path

    @property
    def global_config(self) -> Path:
        return self.root / GLOBAL_CONFIG_RELATIVE

    @property
    def jobs(self) -> Path:
        """The job files: the TUI's default data directory, and the only one whose files get job IDs (J0001)."""
        return self.root / "data" / "jobs"

    @property
    def params(self) -> Path:
        """The Draw Things configurations a job names in ``config_file``."""
        return self.root / "data" / "params"

    @property
    def state(self) -> Path:
        """The state database and the run lock: fixed, since every process must share one lock file."""
        return self.root / "state"

    @property
    def database(self) -> Path:
        return self.state / DATABASE_FILE_NAME

    @property
    def server_token(self) -> Path:
        """The API token ``dtc serve`` generates on first start (0600, gitignored): beside ``global_config``, not in
        the state directory, so the secret stays out of a file that may be shared or committed."""
        return self.root / "config" / SERVER_TOKEN_FILE_NAME


# The project this package is installed in (src/draw_things_control/core/paths.py).
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATHS = ProjectPaths(PROJECT_ROOT)
