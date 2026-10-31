"""SIMPLE IPTV configuration.

All secrets are read from environment variables (or a local .env file).
Nothing in this module is exposed to public templates.
"""
from __future__ import annotations

import os
import secrets
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _secret_key() -> str:
    """Use SECRET_KEY from env, otherwise persist a generated key locally."""
    value = os.getenv("SECRET_KEY", "").strip()
    if value:
        return value
    key_file = BASE_DIR / ".secret_key"
    if key_file.exists():
        stored = key_file.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    generated = secrets.token_hex(48)
    key_file.write_text(generated, encoding="utf-8")
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return generated


def _find_binary(env_name: str, names: list[str], bundled: str | None = None) -> str:
    configured = os.getenv(env_name, "").strip()
    if configured:
        return configured
    if bundled:
        candidate = BASE_DIR / "tools" / "ffmpeg" / bundled
        if candidate.exists():
            return str(candidate)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    # imageio-ffmpeg ships a modern FFmpeg on many Windows machines.
    if names and names[0] == "ffmpeg":
        try:
            import imageio_ffmpeg  # type: ignore

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    return names[0]


def _normalise_admin_path(value: str) -> str:
    value = "/" + value.strip().strip("/")
    if not value.endswith("/"):
        value += "/"
    return value


def _normalise_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    return value


class Config:
    # --- Flask ---------------------------------------------------------
    SECRET_KEY = _secret_key()
    MAX_CONTENT_LENGTH = _int("MAX_CONTENT_LENGTH", 2 * 1024 * 1024 * 1024)
    PERMANENT_SESSION_LIFETIME = _int("SESSION_LIFETIME_MINUTES", 60) * 60
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", False)
    SESSION_COOKIE_NAME = "simple_session"
    JSON_AS_ASCII = False

    # --- Admin ---------------------------------------------------------
    ADMIN_PATH = _normalise_admin_path(
        os.getenv("ADMIN_PATH", "/rJsyNeyajJAJjnsu/admin")
    )
    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "YwyklHfx").strip() or "YwyklHfx"
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "HsuahNItHIYFjY?!Jg@")
    ADMIN_SESSION_MINUTES = _int("ADMIN_SESSION_MINUTES", 60)
    LOGIN_MAX_ATTEMPTS = _int("LOGIN_MAX_ATTEMPTS", 5)
    LOGIN_WINDOW_MINUTES = _int("LOGIN_WINDOW_MINUTES", 15)
    LOGIN_LOCK_MINUTES = _int("LOGIN_LOCK_MINUTES", 15)

    # --- Paths ---------------------------------------------------------
    BASE_DIR = BASE_DIR
    STORAGE_DIR = Path(os.getenv("STORAGE_DIR", str(BASE_DIR / "storage")))
    HLS_DIR = Path(os.getenv("HLS_DIR", str(BASE_DIR / "hls")))
    LOG_DIR = Path(os.getenv("LOG_DIR", str(BASE_DIR / "logs")))
    INSTANCE_DIR = BASE_DIR
    DATABASE_URL = os.getenv(
        "DATABASE_URL", f"sqlite:///{(BASE_DIR / 'simple.db').as_posix()}"
    )
    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # --- Media tools ---------------------------------------------------
    FFMPEG_BIN = _find_binary("FFMPEG_BIN", ["ffmpeg"], "ffmpeg.exe")
    FFPROBE_BIN = _find_binary("FFPROBE_BIN", ["ffprobe"], "ffprobe.exe")
    YTDLP_BIN = os.getenv("YTDLP_BIN", "yt-dlp")
    YTDLP_ENABLED = _bool("YTDLP_ENABLED", True)
    YTDLP_CACHE_TTL = _int("YTDLP_CACHE_TTL", 1800)
    YTDLP_SOCKET_TIMEOUT = _int("YTDLP_SOCKET_TIMEOUT", 25)
    FFMPEG_PROBE_TIMEOUT = _int("FFMPEG_PROBE_TIMEOUT", 40)

    # --- Output --------------------------------------------------------
    OUTPUT_WIDTH = _int("OUTPUT_WIDTH", 1280)
    OUTPUT_HEIGHT = _int("OUTPUT_HEIGHT", 720)
    OUTPUT_FPS = _int("OUTPUT_FPS", 30)
    VIDEO_BITRATE = os.getenv("VIDEO_BITRATE", "2200k")
    AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "128k")
    HLS_TIME = _int("HLS_TIME", 2)
    HLS_LIST_SIZE = _int("HLS_LIST_SIZE", 10)
    HLS_DIR_MAX_AGE = _int("HLS_DIR_MAX_AGE", 300)

    # --- Network / security -------------------------------------------
    PUBLIC_BASE_URL = _normalise_base_url(os.getenv("PUBLIC_BASE_URL", ""))
    ALLOWED_URL_PORTS = {80, 443}
    URL_TIMEOUT = _int("URL_TIMEOUT", 20)
    MAX_URL_LENGTH = 2048
    MAX_VIDEO_UPLOAD = _int("MAX_VIDEO_UPLOAD", 1024 * 1024 * 1024)
    MAX_AUDIO_UPLOAD = _int("MAX_AUDIO_UPLOAD", 200 * 1024 * 1024)
    MAX_IMAGE_UPLOAD = _int("MAX_IMAGE_UPLOAD", 20 * 1024 * 1024)

    # --- Engine --------------------------------------------------------
    ENGINE_ENABLED = _bool("ENGINE_ENABLED", True)
    ENGINE_RETRIES = _int("ENGINE_RETRIES", 2)
    ENGINE_POLL_INTERVAL = float(os.getenv("ENGINE_POLL_INTERVAL", "0.5"))
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

    # --- Formats -------------------------------------------------------
    VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".ts"}
    AUDIO_EXTENSIONS = {".mp3", ".wav", ".opus", ".flac", ".m4a", ".aac", ".ogg"}
    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
    LOGO_SIZE = (342, 74)


def is_windows() -> bool:
    return sys.platform.startswith("win")
