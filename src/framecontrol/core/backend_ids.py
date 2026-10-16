"""Canonical identifiers for the installed job backends."""

DRAW_THINGS_BACKEND = "draw-things-cli"
H3_BACKEND = "h3-metal-cli"

# Keep the externally visible ordering stable as more backends are added.
KNOWN_BACKENDS = (DRAW_THINGS_BACKEND, H3_BACKEND)
