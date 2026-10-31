"""Shared HLS directory management, viewer tracking and cleanup."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from config import Config
from ffmpeg_manager import ffmpeg_manager
from security import SecurityError, is_safe_stream_segment, safe_join
from utils import get_logger

logger = get_logger("stream")


class ViewerTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._viewers: dict[str, dict[str, Any]] = {}

    def touch(self, viewer_id: str, ip: str = "", user_agent: str = "") -> None:
        now = time.monotonic()
        with self._lock:
            row = self._viewers.get(viewer_id)
            if row is None:
                self._viewers[viewer_id] = {
                    "first_seen": now,
                    "last_seen": now,
                    "ip": ip,
                    "user_agent": user_agent[:250],
                }
            else:
                row["last_seen"] = now
                row["ip"] = ip or row["ip"]
                row["user_agent"] = user_agent[:250] or row["user_agent"]

    def count(self, ttl: float = 45.0) -> int:
        now = time.monotonic()
        with self._lock:
            stale = [key for key, row in self._viewers.items() if now - row["last_seen"] > ttl]
            for key in stale:
                self._viewers.pop(key, None)
            return len(self._viewers)

    def cleanup(self, ttl: float = 300.0) -> None:
        now = time.monotonic()
        with self._lock:
            stale = [key for key, row in self._viewers.items() if now - row["last_seen"] > ttl]
            for key in stale:
                self._viewers.pop(key, None)

    def snapshot(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            return [
                {
                    "id": key,
                    "ip": row["ip"],
                    "user_agent": row["user_agent"],
                    "connected_seconds": int(now - row["first_seen"]),
                    "last_seen_seconds": int(now - row["last_seen"]),
                }
                for key, row in self._viewers.items()
            ]


class StreamManager:
    def __init__(self) -> None:
        self.viewers = ViewerTracker()
        self._cleanup_thread: threading.Thread | None = None
        self._stop = threading.Event()

    def hls_dir(self) -> Path:
        path = Config.HLS_DIR / "stream"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def segment_path(self, name: str) -> Path:
        if not is_safe_stream_segment(name):
            raise SecurityError("Недопустимое имя файла потока.")
        return safe_join(self.hls_dir(), name)

    def playlist_text(self) -> str:
        return ffmpeg_manager.playlist_text()

    def ready(self) -> bool:
        return ffmpeg_manager.hls_ready()

    def stats(self) -> dict[str, Any]:
        stats = ffmpeg_manager.hls_stats()
        playlist = self.hls_dir() / "index.m3u8"
        age = None
        if playlist.exists():
            age = max(0.0, time.time() - playlist.stat().st_mtime)
        stats["playlist_age"] = age
        stats["viewers"] = self.viewers.count()
        return stats

    def cleanup(self) -> None:
        removed = ffmpeg_manager.cleanup_hls()
        if removed:
            logger.debug("Удалено устаревших HLS-сегментов: %s", removed)
        self.viewers.cleanup()

    def start_cleanup(self) -> None:
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(20):
                try:
                    self.cleanup()
                except Exception:
                    logger.exception("Ошибка очистки HLS")

        self._cleanup_thread = threading.Thread(target=loop, name="simple-hls-cleanup", daemon=True)
        self._cleanup_thread.start()

    def stop_cleanup(self) -> None:
        self._stop.set()
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=2)
            self._cleanup_thread = None


stream_manager = StreamManager()
