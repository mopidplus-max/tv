"""WSGI entry point used by waitress inside the container image."""
from __future__ import annotations

from app import create_app

# create_app() starts the single shared broadcast engine when ENGINE_ENABLED
# is true, which is the default. Waitress serves this one process, so exactly
# one FFmpeg broadcast engine exists per container.
app = create_app()
