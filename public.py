"""Public channel pages, HLS delivery, M3U, EPG and viewer presence."""
from __future__ import annotations

import io
from datetime import datetime, timedelta
from pathlib import Path

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from sqlalchemy import select

import scheduler
from config import Config
from database import Program, db, get_setting, log_event
from epg import build_epg_gzip, build_epg_xml
from playlist import build_m3u, playlist_filename, public_base_url, stream_url
from security import SecurityError, is_safe_stream_segment, safe_join, validate_viewer_id
from stream_manager import stream_manager
from utils import fmt_duration, fmt_time, now_local
from video_engine import BroadcastEngine

public_bp = Blueprint("public", __name__)


def _engine() -> BroadcastEngine | None:
    return current_app.extensions.get("simple_engine")


def _mode() -> str:
    return get_setting("channel_mode", "on") or "on"


def _channel() -> dict:
    return {
        "name": get_setting("channel_name", "SIMPLE") or "SIMPLE",
        "description": get_setting("channel_description", "") or "",
        "id": get_setting("channel_id", "simple") or "simple",
        "logo": url_for("public.logo"),
    }


def _current_and_next() -> tuple[dict | None, dict | None, dict]:
    now = now_local()
    mode = _mode()
    engine = _engine()
    status = engine.status() if engine else {}
    current_occ = scheduler.active_occurrence(now)
    next_occ = scheduler.next_occurrence(now)
    current: dict | None = None
    if mode == "maintenance":
        current = {
            "title": "Профилактика",
            "description": get_setting("maintenance_message", ""),
            "start": None,
            "end": None,
            "program_id": None,
        }
    elif mode == "off":
        current = {
            "title": "Канал временно не работает",
            "description": "Эфир выключен администратором.",
            "start": None,
            "end": None,
            "program_id": None,
        }
    elif current_occ:
        current = {
            "title": current_occ.title,
            "description": current_occ.description,
            "start": current_occ.start.isoformat(),
            "end": current_occ.end.isoformat(),
            "program_id": current_occ.program_id,
        }
    elif status.get("program_title"):
        current = {
            "title": status.get("program_title", ""),
            "description": status.get("program_description", ""),
            "start": status.get("program_started_at"),
            "end": status.get("program_ends_at"),
            "program_id": status.get("program_id"),
        }
    following: dict | None = None
    if next_occ:
        following = {
            "title": next_occ.title,
            "description": next_occ.description,
            "start": next_occ.start.isoformat(),
            "end": next_occ.end.isoformat(),
            "program_id": next_occ.program_id,
        }
    elif status.get("next_program_title"):
        following = {"title": status.get("next_program_title", ""), "start": None, "end": None}
    return current, following, status


def _progress(current: dict | None, status: dict) -> dict:
    if not current:
        return {"percent": 0.0, "elapsed": 0.0, "remaining": None, "label": "—"}
    start = current.get("start")
    end = current.get("end")
    if start and end:
        try:
            start_dt = datetime.fromisoformat(start)
            end_dt = datetime.fromisoformat(end)
            total = max(1.0, (end_dt - start_dt).total_seconds())
            elapsed = max(0.0, (now_local() - start_dt).total_seconds())
            return {
                "percent": round(min(100.0, elapsed / total * 100), 1),
                "elapsed": round(elapsed, 1),
                "remaining": max(0.0, round(total - elapsed, 1)),
                "label": f"{fmt_duration(elapsed)} / {fmt_duration(total)}",
            }
        except (TypeError, ValueError):
            pass
    return {
        "percent": float(status.get("progress", 0) or 0),
        "elapsed": float(status.get("elapsed", 0) or 0),
        "remaining": status.get("remaining"),
        "label": "LIVE",
    }


def _public_context() -> dict:
    current, following, status = _current_and_next()
    return {
        "channel": _channel(),
        "mode": _mode(),
        "current": current,
        "next": following,
        "progress": _progress(current, status),
        "status": status,
        "stream_url": url_for("public.live_playlist"),
        "base_url": public_base_url(request),
        "maintenance_message": get_setting("maintenance_message", ""),
        "now": now_local(),
    }


@public_bp.route("/")
def index():
    context = _public_context()
    if context["mode"] == "off":
        return render_template("public/offline.html", **context)
    if context["mode"] == "maintenance":
        return render_template("public/maintenance.html", **context)
    upcoming = scheduler.upcoming(days=2)
    if not upcoming:
        upcoming = scheduler.schedule_epg_occurrences(days=1)
    context["upcoming"] = upcoming[:8]
    return render_template("public/index.html", **context)


@public_bp.route("/tv")
def tv_guide():
    context = _public_context()
    occurrences = scheduler.upcoming(days=7)
    if not occurrences:
        occurrences = scheduler.schedule_epg_occurrences(days=7)
    grouped: dict[str, list] = {}
    for item in occurrences:
        key = item.start.strftime("%Y-%m-%d")
        grouped.setdefault(key, []).append(item)
    context.update(
        {
            "grouped": grouped,
            "timeline": scheduler.timeline_for_day(now_local().date()),
            "days": sorted(grouped.keys()),
        }
    )
    return render_template("public/schedule.html", **context)


@public_bp.route("/live/index.m3u8")
def live_playlist():
    path = stream_manager.hls_dir() / "index.m3u8"
    if not path.is_file():
        abort(404)
    response = send_file(
        path,
        mimetype="application/vnd.apple.mpegurl",
        conditional=True,
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@public_bp.route("/live/<path:name>")
def live_segment(name: str):
    try:
        if not is_safe_stream_segment(name):
            abort(404)
        path = stream_manager.segment_path(name)
    except SecurityError:
        abort(404)
    if not path.is_file():
        abort(404)
    response = send_file(
        path,
        mimetype="video/mp2t",
        conditional=True,
        max_age=60,
    )
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@public_bp.route("/playlist.m3u")
def playlist():
    base = public_base_url(request)
    content = build_m3u(base)
    response = Response(content, mimetype="audio/x-mpegurl")
    response.headers["Content-Disposition"] = f'inline; filename="{playlist_filename()}"'
    response.headers["Cache-Control"] = "no-cache"
    return response


@public_bp.route("/epg.xml")
def epg_xml():
    base = public_base_url(request)
    content = build_epg_xml(base, days=7)
    response = Response(content, mimetype="application/xml")
    response.headers["Content-Disposition"] = 'inline; filename="epg.xml"'
    response.headers["Cache-Control"] = "no-cache"
    return response


@public_bp.route("/epg.xml.gz")
def epg_gz():
    base = public_base_url(request)
    content = build_epg_gzip(base, days=7)
    response = Response(content, mimetype="application/gzip")
    response.headers["Content-Disposition"] = 'inline; filename="epg.xml.gz"'
    response.headers["Cache-Control"] = "no-cache"
    return response


@public_bp.route("/logo.png")
def logo():
    relative = get_setting("logo_file", "")
    if relative:
        try:
            path = safe_join(Config.STORAGE_DIR, relative)
            if path.is_file():
                return send_file(path, mimetype="image/png", max_age=3600)
        except SecurityError:
            pass
    return _default_logo()


def _default_logo():
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGBA", (342, 74), (8, 11, 16, 255))
    draw = ImageDraw.Draw(image)
    font = None
    for candidate in (
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            if Path(candidate).is_file():
                font = ImageFont.truetype(candidate, 40)
                break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    text = "SIMPLE"
    bbox = draw.textbbox((0, 0), text, font=font)
    draw.text(
        ((342 - (bbox[2] - bbox[0])) / 2, (74 - (bbox[3] - bbox[1])) / 2 - 2),
        text,
        font=font,
        fill=(244, 247, 252, 255),
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return send_file(buffer, mimetype="image/png", max_age=600)


@public_bp.route("/media/cover/<int:program_id>")
def program_cover(program_id: int):
    program = db.session.get(Program, program_id)
    if program is None or not program.cover_path:
        abort(404)
    try:
        path = safe_join(Config.STORAGE_DIR, program.cover_path)
    except SecurityError:
        abort(404)
    if not path.is_file():
        abort(404)
    return send_file(path, conditional=True, max_age=3600)


@public_bp.route("/api/public/now")
def api_now():
    context = _public_context()
    return jsonify(
        {
            "ok": True,
            "channel": context["channel"],
            "mode": context["mode"],
            "current": context["current"],
            "next": context["next"],
            "progress": context["progress"],
            "stream_url": context["stream_url"],
            "viewers": stream_manager.viewers.count(),
            "engine": {
                "state": context["status"].get("state", ""),
                "ffmpeg_running": context["status"].get("ffmpeg_running", False),
                "hls_ready": context["status"].get("hls_ready", False),
            },
            "server_time": now_local().isoformat(),
        }
    )


@public_bp.route("/api/public/schedule")
def api_schedule():
    try:
        requested = int(request.args.get("days", 7) or 7)
    except (TypeError, ValueError):
        requested = 7
    days = max(1, min(14, requested))
    items = scheduler.upcoming(days=days)
    if not items:
        items = scheduler.schedule_epg_occurrences(days=days)
    return jsonify({"ok": True, "items": [item.to_dict() for item in items]})


@public_bp.route("/api/public/heartbeat", methods=["POST"])
def heartbeat():
    payload = request.get_json(silent=True) or {}
    try:
        viewer_id = validate_viewer_id(payload.get("id"))
    except SecurityError:
        return jsonify({"ok": False, "error": "invalid viewer"}), 400
    stream_manager.viewers.touch(
        viewer_id,
        ip=request.remote_addr or "",
        user_agent=request.headers.get("User-Agent", ""),
    )
    return jsonify({"ok": True, "viewers": stream_manager.viewers.count()})


@public_bp.route("/api/public/viewers")
def viewers():
    return jsonify({"ok": True, "viewers": stream_manager.viewers.count()})


@public_bp.route("/healthz")
def healthz():
    return jsonify(
        {
            "ok": True,
            "mode": _mode(),
            "hls": stream_manager.ready(),
            "time": now_local().isoformat(),
        }
    )


@public_bp.route("/robots.txt")
def robots():
    response = Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@public_bp.route("/favicon.svg")
def favicon():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="16" fill="#0b0f15"/>'
        '<path d="M17 20h30v24H17z" fill="none" stroke="#6d7cff" stroke-width="3"/>'
        '<path d="M27 27l12 5-12 5z" fill="#eaf0ff"/></svg>'
    )
    return Response(svg, mimetype="image/svg+xml")
