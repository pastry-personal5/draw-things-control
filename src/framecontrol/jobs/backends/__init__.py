"""The installed backend registry."""

from framecontrol.core.paths import DEFAULT_PATHS
from framecontrol.jobs.backend import BackendRegistry
from framecontrol.jobs.backends.draw_things import DrawThingsBackend
from framecontrol.jobs.backends.h3 import H3Backend, H3Runtime

DEFAULT_BACKENDS = BackendRegistry((DrawThingsBackend(), H3Backend(H3Runtime(DEFAULT_PATHS.h3_executable, DEFAULT_PATHS.h3_model_directory))))

__all__ = ["DEFAULT_BACKENDS", "DrawThingsBackend", "H3Backend", "H3Runtime"]
