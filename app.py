"""SIMPLE IPTV application entry point."""
from __future__ import annotations

import atexit
import logging
import os
import sys
from datetime import datetime

from flask import Flask, jsonify, render_template, request, session

import monitoring
from admin import admin_bp
from auth import csrf_token
from config import Config
from database import db, get_setting, init_database, log_event
from ffmpeg_manager import ffmpeg_manager
from public import public_bp
from stream_manager import stream_manager
from utils import (
    fmt_dt,
    fmt_duration,
    fmt_time,
    get_logger,
    human_size,
    now_local,
    relative_time,
    setup_logging,
)
from video_engine import BroadcastEngine

logger = get_logger("app")


def create_app(start_engine: bool | None = None) -> Flask:
    setup_logging(Config.LOG_DIR, Config.LOG_LEVEL)
    app = Flask(__name__)
    app.config.from_object(Config)
    app.config["MAX_CONTENT_LENGTH"] = Config.MAX_CONTENT_LENGTH
    app.json.ensure_ascii = False

    init_database(app)
    monitoring.init_monitoring()

    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp, url_prefix=Config.ADMIN_PATH)

    def to_datetime(value):
        """Convert ISO strings coming from JSON/engine state into datetimes."""
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None

    app.add_template_filter(to_datetime, "to_datetime")

    engine = BroadcastEngine(app)
    app.extensions["simple_engine"] = engine

    @app.context_processor
    def inject_globals():
        try:
            channel_name = get_setting("channel_name", "SIMPLE") or "SIMPLE"
            channel_mode = get_setting("channel_mode", "on") or "on"
        except Exception:
            channel_name = "SIMPLE"
            channel_mode = "on"
        return {
            "csrf_token": csrf_token,
            "channel_name": channel_name,
            "channel_mode": channel_mode,
            "current_year": datetime.now().year,
            "fmt_dt": fmt_dt,
            "fmt_time": fmt_time,
            "fmt_duration": fmt_duration,
            "human_size": human_size,
            "relative_time": relative_time,
            "mode_label": {
                "on": "В эфире",
                "off": "Выключен",
                "maintenance": "Профилактика",
            },
        }

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https: http:; "
            "media-src 'self' blob: https: http:; "
            "connect-src 'self'; "
            "font-src 'self' data:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'",
        )
        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    @app.errorhandler(400)
    def bad_request(error):
        message = getattr(error, "description", "Некорректный запрос.")
        if _wants_json():
            return jsonify({"ok": False, "error": message}), 400
        return render_template("public/error.html", code=400, message=message), 400

    @app.errorhandler(403)
    def forbidden(error):
        if _wants_json():
            return jsonify({"ok": False, "error": "Доступ запрещён."}), 403
        return render_template("public/error.html", code=403, message="Доступ запрещён."), 403

    @app.errorhandler(404)
    def not_found(error):
        if _wants_json():
            return jsonify({"ok": False, "error": "Не найдено."}), 404
        return render_template("public/error.html", code=404, message="Страница не найдена."), 404

    @app.errorhandler(413)
    def too_large(error):
        if _wants_json():
            return jsonify({"ok": False, "error": "Файл слишком большой."}), 413
        return (
            render_template(
                "public/error.html",
                code=413,
                message="Загружаемый файл превышает допустимый размер.",
            ),
            413,
        )

    @app.errorhandler(500)
    def server_error(error):
        logger.exception("Внутренняя ошибка сервера: %s", error)
        if _wants_json():
            return jsonify({"ok": False, "error": "Внутренняя ошибка сервера."}), 500
        return (
            render_template(
                "public/error.html",
                code=500,
                message="Внутренняя ошибка сервера. Подробности записаны в журнал.",
            ),
            500,
        )

    def _wants_json() -> bool:
        return (
            request.path.endswith("/api/status")
            or request.path.endswith("/api/monitoring")
            or request.path.endswith("/api/public/now")
            or request.is_json
            or "application/json" in (request.headers.get("Accept") or "")
        )

    # Make the helper visible to the nested error handlers above.
    app.extensions["wants_json"] = _wants_json

    stream_manager.start_cleanup()

    should_start = Config.ENGINE_ENABLED if start_engine is None else start_engine
    if should_start:
        _start_engine(app)
    else:
        logger.info("Эфирный движок создан, но не запущен (тестовый режим)")

    with app.app_context():
        log_event("system", "Приложение SIMPLE запущено")
    binaries = ffmpeg_manager.check_binaries()
    for name, info in binaries.items():
        if not info.get("ok"):
            logger.warning("Не найдено: %s (%s)", name, info.get("version"))
    return app


def _start_engine(app: Flask) -> None:
    engine: BroadcastEngine | None = app.extensions.get("simple_engine")
    if engine is None:
        return
    if engine.start():
        logger.info("Эфирный движок запущен")
    else:
        logger.warning("Эфирный движок уже запущен")


def _shutdown(app: Flask) -> None:
    engine: BroadcastEngine | None = app.extensions.get("simple_engine")
    if engine:
        engine.stop()
    stream_manager.stop_cleanup()


def main() -> None:
    app = create_app()
    atexit.register(_shutdown, app)
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    logger.info("SIMPLE доступен по адресу http://%s:%s", host, port)
    try:
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C")
    finally:
        _shutdown(app)


if __name__ == "__main__":
    main()
