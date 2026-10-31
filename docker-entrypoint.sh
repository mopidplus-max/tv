#!/bin/sh
# Runs on EVERY container start. The persistent volume mounted at /data
# hides the directories created at build time, so they are recreated here.
# The app may run as a non-root user, so every step is best-effort: when a
# folder cannot be fixed we still try to start and let the app report it.
mkdir -p "${STORAGE_DIR:-/data/storage}" "${HLS_DIR:-/data/hls}" "${LOG_DIR:-/data/logs}" 2>/dev/null || true
chown -R "$(id -u):$(id -g)" /data 2>/dev/null || true
chmod -R u+rwX /data 2>/dev/null || true

exec waitress-serve --host=0.0.0.0 --port="${PORT:-5000}" --threads=8 wsgi:app
