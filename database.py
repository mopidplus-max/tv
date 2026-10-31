"""SQLite database, models and small persistence helpers."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from config import Config
from utils import clean_text, get_logger, now_local, parse_bool, parse_int

db = SQLAlchemy()
logger = get_logger("database")


DEFAULT_SETTINGS: dict[str, str] = {
    "channel_name": "SIMPLE",
    "channel_id": "simple",
    "channel_description": "Автоматический интернет-телеканал SIMPLE",
    "channel_mode": "on",  # on | off | maintenance
    "logo_enabled": "1",
    "logo_file": "",
    "bumpers_enabled": "1",
    "bumper_probability": "100",
    "ads_enabled": "1",
    "ad_after_video_probability": "0",
    "ad_after_program": "1",
    "ad_block_size": "1",
    "maintenance_image": "",
    "maintenance_video": "",
    "maintenance_music": "",
    "maintenance_music_enabled": "1",
    "maintenance_message": "Канал находится на техническом обслуживании. Мы скоро вернёмся.",
    "standby_text": "SIMPLE — скоро в эфире",
    "output_width": "1280",
    "output_height": "720",
    "output_fps": "30",
    "video_bitrate": "2200k",
    "audio_bitrate": "128k",
    "hls_time": "2",
    "hls_list_size": "10",
    "respect_schedule": "1",
    "public_base_url": "",
    "max_retries": "2",
    "default_program_minutes": "30",
}


def _as_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else Config.STORAGE_DIR / path


def ensure_directories() -> None:
    for name in ("videos", "ads", "bumpers", "logo", "maintenance", "intros", "covers", "tmp"):
        (Config.STORAGE_DIR / name).mkdir(parents=True, exist_ok=True)
    (Config.HLS_DIR / "stream").mkdir(parents=True, exist_ok=True)
    Config.LOG_DIR.mkdir(parents=True, exist_ok=True)


class Setting(db.Model):
    __tablename__ = "settings"
    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(db.DateTime, default=now_local, onupdate=now_local, nullable=False)


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=now_local, nullable=False)
    last_login = db.Column(db.DateTime)


class Media(db.Model):
    __tablename__ = "media"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300), nullable=False)
    source_type = db.Column(db.String(20), nullable=False, default="local")  # local|url|hls|ytdlp
    source_url = db.Column(db.Text)
    file_path = db.Column(db.Text)
    duration = db.Column(db.Float)
    width = db.Column(db.Integer)
    height = db.Column(db.Integer)
    fps = db.Column(db.Float)
    vcodec = db.Column(db.String(40))
    acodec = db.Column(db.String(40))
    has_audio = db.Column(db.Boolean, default=False, nullable=False)
    aspect = db.Column(db.String(30))
    pix_fmt = db.Column(db.String(40))
    sample_rate = db.Column(db.Integer)
    size_bytes = db.Column(db.BigInteger)
    mime = db.Column(db.String(100))
    category = db.Column(db.String(80), default="", nullable=False)
    tags = db.Column(db.String(300), default="", nullable=False)
    status = db.Column(db.String(20), default="ok", nullable=False)
    last_error = db.Column(db.Text)
    last_checked = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=now_local, nullable=False, index=True)

    @property
    def is_local(self) -> bool:
        return self.source_type == "local" and bool(self.file_path)

    @property
    def is_stream(self) -> bool:
        return self.source_type in {"url", "hls", "ytdlp"}

    @property
    def absolute_path(self) -> Path | None:
        return _as_path(self.file_path)

    @property
    def source_label(self) -> str:
        return {
            "local": "Файл",
            "url": "Ссылка",
            "hls": "HLS",
            "ytdlp": "yt-dlp",
        }.get(self.source_type, self.source_type)

    @property
    def resolution_label(self) -> str:
        if self.width and self.height:
            return f"{self.width}×{self.height}"
        return "—"

    @property
    def duration_label(self) -> str:
        from utils import fmt_duration

        return fmt_duration(self.duration)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "source_type": self.source_type,
            "source_label": self.source_label,
            "source_url": self.source_url,
            "file_path": self.file_path,
            "duration": self.duration,
            "duration_label": self.duration_label,
            "width": self.width,
            "height": self.height,
            "resolution_label": self.resolution_label,
            "fps": self.fps,
            "vcodec": self.vcodec,
            "acodec": self.acodec,
            "has_audio": self.has_audio,
            "aspect": self.aspect,
            "pix_fmt": self.pix_fmt,
            "sample_rate": self.sample_rate,
            "size_bytes": self.size_bytes,
            "category": self.category,
            "tags": self.tags,
            "status": self.status,
            "last_error": self.last_error,
            "last_checked": self.last_checked.isoformat() if self.last_checked else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Program(db.Model):
    __tablename__ = "programs"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300), nullable=False)
    description = db.Column(db.Text, default="", nullable=False)
    cover_path = db.Column(db.Text)
    position = db.Column(db.Integer, default=0, nullable=False, index=True)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=now_local, nullable=False)
    updated_at = db.Column(db.DateTime, default=now_local, onupdate=now_local, nullable=False)

    items = db.relationship(
        "ProgramItem",
        back_populates="program",
        cascade="all, delete-orphan",
        order_by="ProgramItem.position, ProgramItem.id",
        lazy="selectin",
    )
    schedule_entries = db.relationship(
        "ScheduleEntry", back_populates="program", cascade="all, delete-orphan"
    )

    @property
    def total_duration(self) -> float:
        return sum(float(item.media.duration or 0) for item in self.items if item.media)

    @property
    def duration_label(self) -> str:
        from utils import fmt_duration

        return fmt_duration(self.total_duration)

    @property
    def cover_url(self) -> str | None:
        return f"/media/cover/{self.id}" if self.cover_path else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "position": self.position,
            "enabled": self.enabled,
            "duration": self.total_duration,
            "duration_label": self.duration_label,
            "items": [item.to_dict() for item in self.items],
        }


class ProgramItem(db.Model):
    __tablename__ = "program_items"
    id = db.Column(db.Integer, primary_key=True)
    program_id = db.Column(db.Integer, db.ForeignKey("programs.id", ondelete="CASCADE"), nullable=False, index=True)
    media_id = db.Column(db.Integer, db.ForeignKey("media.id"), nullable=False, index=True)
    position = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=now_local, nullable=False)

    program = db.relationship("Program", back_populates="items")
    media = db.relationship("Media", lazy="joined")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "program_id": self.program_id,
            "position": self.position,
            "media": self.media.to_dict() if self.media else None,
        }


class ScheduleEntry(db.Model):
    __tablename__ = "schedule_entries"
    id = db.Column(db.Integer, primary_key=True)
    program_id = db.Column(db.Integer, db.ForeignKey("programs.id", ondelete="CASCADE"), nullable=False, index=True)
    start_at = db.Column(db.DateTime, nullable=False, index=True)
    duration_minutes = db.Column(db.Integer, default=30, nullable=False)
    repeat_days = db.Column(db.String(30), default="", nullable=False)  # 0=Mon ... 6=Sun
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=now_local, nullable=False)

    program = db.relationship("Program", back_populates="schedule_entries", lazy="joined")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "program_id": self.program_id,
            "program_title": self.program.title if self.program else "—",
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "duration_minutes": self.duration_minutes,
            "repeat_days": self.repeat_days,
            "enabled": self.enabled,
        }


class Bumper(db.Model):
    __tablename__ = "bumpers"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300), nullable=False)
    file_path = db.Column(db.Text, nullable=False)
    duration = db.Column(db.Float)
    width = db.Column(db.Integer)
    height = db.Column(db.Integer)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    probability = db.Column(db.Integer, default=100, nullable=False)
    created_at = db.Column(db.DateTime, default=now_local, nullable=False)

    @property
    def absolute_path(self) -> Path:
        return Config.STORAGE_DIR / self.file_path

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "enabled": self.enabled,
            "probability": self.probability,
        }


class Ad(db.Model):
    __tablename__ = "ads"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300), nullable=False)
    file_path = db.Column(db.Text, nullable=False)
    duration = db.Column(db.Float)
    width = db.Column(db.Integer)
    height = db.Column(db.Integer)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=now_local, nullable=False)

    @property
    def absolute_path(self) -> Path:
        return Config.STORAGE_DIR / self.file_path

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "enabled": self.enabled,
        }


class EventLog(db.Model):
    __tablename__ = "event_log"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=now_local, nullable=False, index=True)
    level = db.Column(db.String(20), default="info", nullable=False, index=True)
    category = db.Column(db.String(40), default="system", nullable=False, index=True)
    message = db.Column(db.Text, nullable=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "level": self.level,
            "category": self.category,
            "message": self.message,
        }


class PlaybackState(db.Model):
    __tablename__ = "playback_state"
    id = db.Column(db.Integer, primary_key=True, default=1)
    current_program_id = db.Column(db.Integer, db.ForeignKey("programs.id", ondelete="SET NULL"))
    current_item_id = db.Column(db.Integer, db.ForeignKey("program_items.id", ondelete="SET NULL"))
    current_media_id = db.Column(db.Integer, db.ForeignKey("media.id", ondelete="SET NULL"))
    phase = db.Column(db.String(30), default="idle", nullable=False)
    item_started_at = db.Column(db.DateTime)
    program_started_at = db.Column(db.DateTime)
    last_bumper_id = db.Column(db.Integer)
    last_ad_id = db.Column(db.Integer)
    updated_at = db.Column(db.DateTime, default=now_local, onupdate=now_local, nullable=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_program_id": self.current_program_id,
            "current_item_id": self.current_item_id,
            "current_media_id": self.current_media_id,
            "phase": self.phase,
            "item_started_at": self.item_started_at.isoformat() if self.item_started_at else None,
            "program_started_at": self.program_started_at.isoformat() if self.program_started_at else None,
            "last_bumper_id": self.last_bumper_id,
            "last_ad_id": self.last_ad_id,
        }


class LoginAttempt(db.Model):
    __tablename__ = "login_attempts"
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(255), unique=True, nullable=False, index=True)
    attempts = db.Column(db.Integer, default=0, nullable=False)
    first_attempt = db.Column(db.DateTime, default=now_local, nullable=False)
    last_attempt = db.Column(db.DateTime, default=now_local, nullable=False)
    locked_until = db.Column(db.DateTime)


# ---------------------------------------------------------------------------
# Helpers (all assume an active Flask application context)
# ---------------------------------------------------------------------------
def get_setting(key: str, default: str = "") -> str:
    row = db.session.get(Setting, key)
    if row is None:
        return default
    return row.value


def get_int_setting(key: str, default: int = 0) -> int:
    return parse_int(get_setting(key, str(default)), default)


def get_bool_setting(key: str, default: bool = False) -> bool:
    return parse_bool(get_setting(key, "1" if default else "0"), default)


def set_setting(key: str, value: Any) -> None:
    row = db.session.get(Setting, key)
    text = "" if value is None else str(value)
    if row is None:
        db.session.add(Setting(key=key, value=text))
    else:
        row.value = text
    db.session.commit()


def set_settings(values: dict[str, Any]) -> None:
    for key, value in values.items():
        row = db.session.get(Setting, key)
        text = "" if value is None else str(value)
        if row is None:
            db.session.add(Setting(key=key, value=text))
        else:
            row.value = text
    db.session.commit()


def all_settings() -> dict[str, str]:
    rows = db.session.execute(select(Setting)).scalars().all()
    return {row.key: row.value for row in rows}


def log_event(category: str, message: str, level: str = "info") -> None:
    """Write to the file log and the in-app broadcast journal."""
    message = clean_text(message, 4000, "—")
    level = level if level in {"debug", "info", "warning", "error", "critical"} else "info"
    log_method = getattr(logger, level, logger.info)
    log_method("[%s] %s", category, message)
    try:
        db.session.add(
            EventLog(level=level, category=clean_text(category, 40, "system"), message=message)
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Не удалось записать событие в журнал")


def get_playback_state() -> PlaybackState:
    state = db.session.get(PlaybackState, 1)
    if state is None:
        state = PlaybackState(id=1, phase="idle")
        db.session.add(state)
        db.session.commit()
    return state


def save_playback_state(**values: Any) -> None:
    state = get_playback_state()
    for key, value in values.items():
        if hasattr(state, key):
            setattr(state, key, value)
    state.updated_at = now_local()
    db.session.commit()


def program_by_id(program_id: int | None) -> Program | None:
    if not program_id:
        return None
    return db.session.get(Program, program_id)


def enabled_programs() -> list[Program]:
    return list(
        db.session.execute(
            select(Program)
            .where(Program.enabled.is_(True))
            .order_by(Program.position.asc(), Program.id.asc())
        )
        .scalars()
        .all()
    )


def media_usage_count(media_id: int) -> int:
    return int(
        db.session.execute(
            select(func.count(ProgramItem.id)).where(ProgramItem.media_id == media_id)
        ).scalar_one()
    )


def active_viewers_count() -> int:
    # Viewer tracking is in-memory in stream_manager; this helper is a DB-safe fallback.
    return 0


def init_database(app: Flask) -> None:
    """Create tables, directories, defaults and the initial admin user."""
    ensure_directories()
    db.init_app(app)
    with app.app_context():
        db.create_all()
        existing = {row.key for row in db.session.execute(select(Setting)).scalars().all()}
        for key, value in DEFAULT_SETTINGS.items():
            if key not in existing:
                db.session.add(Setting(key=key, value=value))
        db.session.commit()
        get_playback_state()
        _seed_admin(app)
        logger.info("База данных готова: %s", Config.SQLALCHEMY_DATABASE_URI)


def _seed_admin(app: Flask) -> None:
    username = app.config.get("ADMIN_USERNAME") or Config.ADMIN_USERNAME
    password = app.config.get("ADMIN_PASSWORD") or Config.ADMIN_PASSWORD
    user = db.session.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if user is None:
        from argon2 import PasswordHasher

        hasher = PasswordHasher(
            time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16
        )
        user = User(username=username, password_hash=hasher.hash(password))
        db.session.add(user)
        db.session.commit()
        logger.info("Создан администратор %s", username)


def cleanup_old_events(days: int = 30) -> int:
    cutoff = now_local() - timedelta(days=max(1, days))
    result = db.session.execute(
        db.delete(EventLog).where(EventLog.created_at < cutoff)
    )
    db.session.commit()
    return int(result.rowcount or 0)
