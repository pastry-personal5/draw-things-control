"""The installed backend registry."""

from draw_things_control.jobs.backend import BackendRegistry
from draw_things_control.jobs.backends.draw_things import DrawThingsBackend

DEFAULT_BACKENDS = BackendRegistry((DrawThingsBackend(),))

__all__ = ["DEFAULT_BACKENDS", "DrawThingsBackend"]
