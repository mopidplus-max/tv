#!/bin/sh
# Runs on EVERY container start. The persistent volume mounted at /data
# hides the directories created at build time, so they are recreated here.
mkdir -p "${STORAGE_DIR:-/data/storage}" "${HLS_DIR:-/data/hls}" "${LOG_DIR:-/data/logs}" /data/storage /data/hls /data/logs || true
chmod -R u+rwX /data || true

exec waitress-serve --host=0.0.0.0 --port="${PORT:-5000}" --threads=8 wsgi:app
