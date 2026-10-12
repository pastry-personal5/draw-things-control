"""The installed backend registry."""

from draw_things_control.core.paths import DEFAULT_PATHS
from draw_things_control.jobs.backend import BackendRegistry
from draw_things_control.jobs.backends.draw_things import DrawThingsBackend
from draw_things_control.jobs.backends.h3 import H3Backend, H3Runtime

DEFAULT_BACKENDS = BackendRegistry((DrawThingsBackend(), H3Backend(H3Runtime(DEFAULT_PATHS.h3_executable, DEFAULT_PATHS.h3_model_directory))))

__all__ = ["DEFAULT_BACKENDS", "DrawThingsBackend", "H3Backend", "H3Runtime"]
