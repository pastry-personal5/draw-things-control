"""Where the project keeps its files, worked out once."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DATABASE_FILE_NAME = "framecontrol.db"
LOCK_FILE_NAME = "run.lock"
SERVER_TOKEN_FILE_NAME = "server-token"
MCP_TOKEN_FILE_NAME = "mcp-token"
TUI_PREFERENCES_FILE_NAME = "tui-preferences.yaml"
TUI_COMMAND_HISTORY_FILE_NAME = "tui-command-history.txt"
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
    def jobs_trash(self) -> Path:
        return self.jobs / ".trash"

    @property
    def jobs_backups(self) -> Path:
        return self.jobs / ".backups"

    @property
    def params(self) -> Path:
        """The Draw Things configurations a job names in ``config_file``."""
        return self.root / "data" / "params"

    @property
    def timing_data(self) -> Path:
        """The forecast's knob files (tunables and calibration) and the evidence they were learned from (gitignored personal data)."""
        return self.root / "data" / "timing-data-analysis"

    @property
    def h3_executable(self) -> Path:
        """The checked-in H3 executable; jobs never select an executable themselves."""
        return self.root / "contrib" / "backends" / "h3.c" / "h3"

    @property
    def h3_model_directory(self) -> Path:
        """The local H3 model snapshot, deliberately outside job and queue data."""
        return self.root / "contrib" / "backends" / "h3.c" / "MiniMax-H3"

    @property
    def state(self) -> Path:
        """The state database and the run lock: fixed, since every process must share one lock file."""
        return self.root / "state"

    @property
    def database(self) -> Path:
        return self.state / DATABASE_FILE_NAME

    @property
    def server_token(self) -> Path:
        """The API token ``fc serve`` generates on first start (0600, gitignored): beside ``global_config``, not in
        the state directory, so the secret stays out of a file that may be shared or committed."""
        return self.root / "config" / SERVER_TOKEN_FILE_NAME

    @property
    def mcp_token(self) -> Path:
        """The separate credential a remote MCP client presents to the listener."""
        return self.root / "config" / MCP_TOKEN_FILE_NAME

    @property
    def tui_preferences(self) -> Path:
        """The terminal UI's own per-machine preferences (gitignored): the verbose level today. Not
        ``global_config``, which is shared with ``fc serve``, hand-edited, and strictly validated."""
        return self.root / "config" / TUI_PREFERENCES_FILE_NAME

    @property
    def tui_command_history(self) -> Path:
        """The commands typed on the terminal UI's command field, one per line, oldest first (gitignored, per machine)."""
        return self.root / "config" / TUI_COMMAND_HISTORY_FILE_NAME


def linked_component(path: Path, root: Path) -> Path | None:
    """First symbolic link from the project root through ``path``, without following it."""
    root = root.absolute()
    try:
        parts = path.absolute().relative_to(root).parts
    except ValueError:
        return path if path.is_symlink() else None
    current = root
    if current.is_symlink():
        return current
    for part in parts:
        current /= part
        if current.is_symlink():
            return current
    return None


# The project this package is installed in (src/framecontrol/core/paths.py).
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATHS = ProjectPaths(PROJECT_ROOT)
