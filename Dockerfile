# SIMPLE IPTV container image.
#
# The application needs FFmpeg/FFprobe at runtime. The Windows binaries in
# tools/ffmpeg are intentionally excluded from the image; the Debian packages
# provide Linux builds instead.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=5000 \
    HOST=0.0.0.0 \
    STORAGE_DIR=/data/storage \
    HLS_DIR=/data/hls \
    LOG_DIR=/data/logs \
    DATABASE_URL=sqlite:////data/simple.db \
    FFMPEG_BIN=/usr/bin/ffmpeg \
    FFPROBE_BIN=/usr/bin/ffprobe \
    ENGINE_ENABLED=true

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN sed -i 's/\r$//' /app/docker-entrypoint.sh && chmod +x /app/docker-entrypoint.sh

# The app runs as uid 1000 on hosting platforms that drop root, so the data
# folders must belong to that user even when the volume is seeded from the image.
RUN mkdir -p /data/storage /data/hls /data/logs \
    && chown -R 1000:1000 /data

# Keep the database, uploads, HLS segments and logs between restarts.
VOLUME ["/data"]

USER 1000:1000

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=6s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '5000') + '/healthz', timeout=4)" || exit 1

ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]
