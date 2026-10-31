"""Hidden SIMPLE administration panel."""
from __future__ import annotations

import mimetypes
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from sqlalchemy import func, or_, select

import scheduler
from auth import (
    authenticate,
    change_password,
    csrf_token,
    current_user,
    is_authenticated,
    login_user,
    logout_user,
    require_csrf,
    session_expired,
    touch_session,
)
from config import Config
from database import (
    Ad,
    Bumper,
    EventLog,
    Media,
    Program,
    ProgramItem,
    ScheduleEntry,
    db,
    enabled_programs,
    get_bool_setting,
    get_int_setting,
    get_setting,
    log_event,
    media_usage_count,
    set_setting,
    set_settings,
)
from epg import build_epg_gzip, build_epg_xml
from ffmpeg_manager import FFmpegError, ffmpeg_manager
from monitoring import system_metrics
from playlist import build_m3u, public_base_url, stream_url
from security import (
    SecurityError,
    sanitize_tags,
    validate_hls_playlist,
    validate_public_url,
    validate_repeat_days,
)
from uploads import (
    UploadError,
    delete_stored,
    save_ad,
    save_audio,
    save_bumper,
    save_cover,
    save_image,
    save_logo,
    save_video,
    suggested_title,
)
from utils import (
    clean_text,
    fmt_duration,
    human_size,
    parse_bool,
    parse_int,
    relative_time,
)
from ytdlp_manager import YtDlpError, ytdlp_manager

admin_bp = Blueprint("admin", __name__)

YTDLP_HOSTS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "vimeo.com",
    "dailymotion.com",
    "twitch.tv",
    "facebook.com",
    "fb.watch",
    "instagram.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "soundcloud.com",
    "rutube.ru",
    "vk.com",
    "ok.ru",
    "bilibili.com",
)


def _engine():
    return current_app.extensions.get("simple_engine")


def _wants_json() -> bool:
    return (
        request.is_json
        or request.args.get("format") == "json"
        or "application/json" in (request.headers.get("Accept") or "")
    )


def _json_ok(message: str = "", **extra):
    payload = {"ok": True, "message": message}
    payload.update(extra)
    return jsonify(payload)


def _json_error(message: str, status: int = 400, **extra):
    payload = {"ok": False, "error": message}
    payload.update(extra)
    return jsonify(payload), status


def _safe_next(value: str | None) -> str:
    if value and value.startswith(Config.ADMIN_PATH) and "://" not in value:
        return value
    return url_for("admin.dashboard")


def _program_or_404(program_id: int) -> Program:
    program = db.session.get(Program, program_id)
    if program is None:
        abort(404)
    return program


def _media_or_404(media_id: int) -> Media:
    media = db.session.get(Media, media_id)
    if media is None:
        abort(404)
    return media


def _is_ytdlp_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == item or host.endswith("." + item) for item in YTDLP_HOSTS)


def _create_ytdlp_media(url: str, title: str, category: str, tags: str) -> Media:
    stream = ytdlp_manager.extract(url)
    probe = None
    try:
        probe = ffmpeg_manager.probe(stream.url, headers=stream.headers, remote=True)
    except FFmpegError:
        pass
    media = Media(
        title=clean_text(title or stream.title or url, 300, "Видео"),
        source_type="ytdlp",
        source_url=url,
        duration=stream.duration or (probe.duration if probe else None),
        width=probe.width if probe else None,
        height=probe.height if probe else None,
        fps=probe.fps if probe else None,
        vcodec=probe.vcodec if probe else None,
        acodec=probe.acodec if probe else None,
        has_audio=bool(stream.audio_url or (probe.has_audio if probe else False)),
        aspect=probe.aspect if probe else None,
        pix_fmt=probe.pix_fmt if probe else None,
        sample_rate=probe.sample_rate if probe else None,
        category=clean_text(category, 80),
        tags=sanitize_tags(tags),
        status="ok",
        last_checked=datetime.now(),
    )
    db.session.add(media)
    db.session.commit()
    log_event("media", f"Добавлен источник yt-dlp: {media.title}")
    return media


def _create_direct_media(url: str, title: str, category: str, tags: str) -> Media:
    validate_public_url(url)
    is_hls = ".m3u8" in url.lower()
    if is_hls:
        validate_hls_playlist(url)
    probe = ffmpeg_manager.probe(url, remote=True)
    media = Media(
        title=clean_text(title or Path(urlparse(url).path).stem or url, 300, "Видео"),
        source_type="hls" if is_hls else "url",
        source_url=url,
        duration=probe.duration,
        width=probe.width,
        height=probe.height,
        fps=probe.fps,
        vcodec=probe.vcodec,
        acodec=probe.acodec,
        has_audio=probe.has_audio,
        aspect=probe.aspect,
        pix_fmt=probe.pix_fmt,
        sample_rate=probe.sample_rate,
        category=clean_text(category, 80),
        tags=sanitize_tags(tags),
        status="ok",
        last_checked=datetime.now(),
    )
    db.session.add(media)
    db.session.commit()
    log_event("media", f"Добавлен прямой источник: {media.title}")
    return media


def _create_media_from_url(url: str, title: str = "", category: str = "", tags: str = "") -> Media:
    url = clean_text(url, Config.MAX_URL_LENGTH)
    if not url:
        raise UploadError("Укажите ссылку на видео.")
    if _is_ytdlp_host(url):
        if not ytdlp_manager.available():
            raise UploadError("yt-dlp не установлен, этот источник недоступен.")
        return _create_ytdlp_media(url, title, category, tags)
    try:
        return _create_direct_media(url, title, category, tags)
    except (SecurityError, FFmpegError, OSError) as direct_error:
        if ytdlp_manager.available():
            try:
                return _create_ytdlp_media(url, title, category, tags)
            except (YtDlpError, SecurityError, OSError):
                pass
        raise UploadError(f"Не удалось прочитать источник: {direct_error}") from direct_error


def _create_local_media(
    file,
    title: str = "",
    category: str = "",
    tags: str = "",
    kind: str = "videos",
) -> Media:
    path, relative, probe, size = save_video(file, kind=kind)
    try:
        media = Media(
            title=clean_text(title or suggested_title(file.filename), 300, "Видео"),
            source_type="local",
            file_path=relative,
            duration=probe.duration,
            width=probe.width,
            height=probe.height,
            fps=probe.fps,
            vcodec=probe.vcodec,
            acodec=probe.acodec,
            has_audio=probe.has_audio,
            aspect=probe.aspect,
            pix_fmt=probe.pix_fmt,
            sample_rate=probe.sample_rate,
            size_bytes=size,
            mime=file.mimetype or mimetypes.guess_type(file.filename or "")[0],
            category=clean_text(category, 80),
            tags=sanitize_tags(tags),
            status="ok",
            last_checked=datetime.now(),
        )
        db.session.add(media)
        db.session.commit()
        log_event("media", f"Загружено видео: {media.title}")
        return media
    except Exception:
        db.session.rollback()
        delete_stored(relative)
        raise


def _add_item(program: Program, media: Media) -> ProgramItem:
    position = max([item.position for item in program.items], default=-1) + 1
    item = ProgramItem(program_id=program.id, media_id=media.id, position=position)
    db.session.add(item)
    db.session.commit()
    log_event("program", f"В передачу «{program.title}» добавлено: {media.title}")
    return item


def _set_media_fields(media: Media, title: str, category: str, tags: str) -> None:
    media.title = clean_text(title, 300, media.title)
    media.category = clean_text(category, 80)
    media.tags = sanitize_tags(tags)
    db.session.commit()


def _remove_old_file(relative: str | None) -> None:
    if relative:
        delete_stored(relative)


def _save_setting_file(setting_key: str, relative: str) -> None:
    old = get_setting(setting_key, "")
    set_setting(setting_key, relative)
    if old and old != relative:
        _remove_old_file(old)


# ---------------------------------------------------------------------------
# Authentication guard
# ---------------------------------------------------------------------------
@admin_bp.before_request
def _guard():
    if request.endpoint == "admin.login":
        if request.method == "POST":
            require_csrf()
        return None
    if not is_authenticated():
        if request.path.endswith("/api/status") or request.is_json:
            return _json_error("unauthorized", 401)
        return redirect(url_for("admin.login", next=request.full_path))
    if session_expired():
        logout_user()
        if request.is_json:
            return _json_error("session_expired", 401)
        return redirect(url_for("admin.login", expired=1))
    touch_session()
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        require_csrf()
    return None


@admin_bp.app_context_processor
def _inject_admin_context():
    return {
        "admin_username": session.get("admin_username", ""),
        "csrf_token": csrf_token,
    }


@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if is_authenticated() and not session_expired():
        return redirect(url_for("admin.dashboard"))
    error = ""
    if request.method == "POST":
        username = clean_text(request.form.get("username"), 120)
        password = request.form.get("password") or ""
        ip = request.remote_addr or "unknown"
        user, message = authenticate(username, password, ip)
        if user:
            login_user(user)
            log_event("auth", f"Вход администратора: {user.username}")
            return redirect(_safe_next(request.args.get("next")))
        error = message
        log_event("auth", f"Неудачная попытка входа: {username or '—'}", "warning")
    if request.args.get("expired"):
        error = "Сессия истекла. Войдите снова."
    return render_template(
        "admin/login.html",
        error=error,
        csrf_token=csrf_token(),
        next_url=request.args.get("next", ""),
    )


@admin_bp.route("/logout", methods=["POST"])
def logout():
    username = session.get("admin_username", "")
    logout_user()
    if username:
        log_event("auth", f"Выход администратора: {username}")
    return redirect(url_for("admin.login"))


# ---------------------------------------------------------------------------
# Dashboard and director panel
# ---------------------------------------------------------------------------
@admin_bp.route("/")
def dashboard():
    engine = _engine()
    status = engine.status() if engine else {}
    current = scheduler.active_occurrence()
    following = scheduler.next_occurrence()
    events = list(
        db.session.execute(
            select(EventLog).order_by(EventLog.created_at.desc(), EventLog.id.desc()).limit(8)
        )
        .scalars()
        .all()
    )
    current_program = db.session.get(Program, status.get("program_id")) if status.get("program_id") else None
    current_items = list(current_program.items) if current_program else []
    programs = enabled_programs()
    return render_template(
        "admin/dashboard.html",
        active_page="dashboard",
        status=status,
        current=current,
        following=following,
        metrics=system_metrics(),
        events=events,
        current_program=current_program,
        current_items=current_items,
        programs=programs,
        mode=get_setting("channel_mode", "on"),
        base_url=public_base_url(request),
        stream_url=stream_url(public_base_url(request)),
    )


@admin_bp.route("/api/status")
def api_status():
    engine = _engine()
    status = engine.status() if engine else {}
    metrics = system_metrics()
    current = scheduler.active_occurrence()
    following = scheduler.next_occurrence()
    return jsonify(
        {
            "ok": True,
            "engine": status,
            "metrics": metrics,
            "current": current.to_dict() if current else None,
            "next": following.to_dict() if following else None,
            "mode": get_setting("channel_mode", "on"),
            "viewers": status.get("viewers", 0),
        }
    )


def _director_action(action: str, message: str, **extra):
    engine = _engine()
    if engine is None or not engine.is_running():
        return _json_error("Эфирный движок не запущен.", 409)
    method = getattr(engine, action, None)
    if method is None:
        return _json_error("Неизвестное действие.", 400)
    result = method(**extra) if extra else method()
    if result is False:
        return _json_error("Действие недоступно в текущем состоянии.", 409)
    log_event("admin", f"{session.get('admin_username')}: {message}")
    return _json_ok(message, status=engine.status())


@admin_bp.route("/director/pause", methods=["POST"])
def director_pause():
    return _director_action("pause", "Эфир поставлен на паузу")


@admin_bp.route("/director/resume", methods=["POST"])
def director_resume():
    return _director_action("resume", "Эфир продолжен")


@admin_bp.route("/director/skip", methods=["POST"])
def director_skip():
    return _director_action("skip", "Текущий ролик пропущен")


@admin_bp.route("/director/restart", methods=["POST"])
def director_restart():
    return _director_action("restart", "Текущий ролик перезапущен")


@admin_bp.route("/director/next-program", methods=["POST"])
def director_next_program():
    return _director_action("next_program", "Переход к следующей передаче")


@admin_bp.route("/director/play-item", methods=["POST"])
def director_play_item():
    item_id = parse_int(request.form.get("item_id") or (request.get_json(silent=True) or {}).get("item_id"), 0)
    if not item_id:
        return _json_error("Не выбран элемент.")
    return _director_action("play_item", f"Выбран следующий ролик #{item_id}", item_id=item_id)


@admin_bp.route("/preview")
def preview():
    engine = _engine()
    status = engine.status() if engine else {}
    current_program = db.session.get(Program, status.get("program_id")) if status.get("program_id") else None
    items = list(current_program.items) if current_program else []
    return render_template(
        "admin/preview.html",
        active_page="preview",
        status=status,
        current=scheduler.active_occurrence(),
        following=scheduler.next_occurrence(),
        current_program=current_program,
        items=items,
        mode=get_setting("channel_mode", "on"),
        stream_url="/live/index.m3u8",
    )


# ---------------------------------------------------------------------------
# Programs
# ---------------------------------------------------------------------------
@admin_bp.route("/programs")
def programs():
    rows = list(
        db.session.execute(select(Program).order_by(Program.position.asc(), Program.id.asc()))
        .scalars()
        .all()
    )
    return render_template(
        "admin/programs.html",
        active_page="programs",
        programs=rows,
        mode=get_setting("channel_mode", "on"),
    )


@admin_bp.route("/programs/new", methods=["GET", "POST"])
def program_new():
    if request.method == "POST":
        title = clean_text(request.form.get("title"), 300)
        if not title:
            flash("Введите название передачи.", "error")
            return render_template("admin/program_new.html", active_page="programs")
        description = clean_text(request.form.get("description"), 4000)
        cover = request.files.get("cover")
        cover_relative = None
        try:
            if cover and cover.filename:
                _, cover_relative, _, _ = save_cover(cover)
            max_position = db.session.execute(select(func.max(Program.position))).scalar() or 0
            program = Program(
                title=title,
                description=description,
                cover_path=cover_relative,
                position=int(max_position) + 1,
                enabled=True,
            )
            db.session.add(program)
            db.session.commit()
            log_event("program", f"Создана передача: {program.title}")
            flash("Передача создана. Добавьте видео.", "success")
            return redirect(url_for("admin.program_edit", program_id=program.id))
        except UploadError as exc:
            flash(str(exc), "error")
        except Exception:
            db.session.rollback()
            _remove_old_file(cover_relative)
            raise
    return render_template("admin/program_new.html", active_page="programs")


@admin_bp.route("/programs/<int:program_id>/edit")
def program_edit(program_id: int):
    program = _program_or_404(program_id)
    media_rows = list(
        db.session.execute(select(Media).order_by(Media.created_at.desc()).limit(200))
        .scalars()
        .all()
    )
    return render_template(
        "admin/program_edit.html",
        active_page="programs",
        program=program,
        media_rows=media_rows,
        mode=get_setting("channel_mode", "on"),
    )


@admin_bp.route("/programs/<int:program_id>/update", methods=["POST"])
def program_update(program_id: int):
    program = _program_or_404(program_id)
    title = clean_text(request.form.get("title"), 300)
    if not title:
        flash("Название передачи не может быть пустым.", "error")
        return redirect(url_for("admin.program_edit", program_id=program.id))
    program.title = title
    program.description = clean_text(request.form.get("description"), 4000)
    program.enabled = parse_bool(request.form.get("enabled"), True)
    cover = request.files.get("cover")
    try:
        if cover and cover.filename:
            _, relative, _, _ = save_cover(cover)
            old = program.cover_path
            program.cover_path = relative
            _remove_old_file(old)
        if parse_bool(request.form.get("remove_cover"), False):
            _remove_old_file(program.cover_path)
            program.cover_path = None
        db.session.commit()
        log_event("program", f"Передача обновлена: {program.title}")
        flash("Изменения сохранены.", "success")
    except UploadError as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.program_edit", program_id=program.id))


@admin_bp.route("/programs/<int:program_id>/delete", methods=["POST"])
def program_delete(program_id: int):
    program = _program_or_404(program_id)
    if program.schedule_entries:
        flash("Передача используется в расписании. Сначала удалите её из расписания.", "error")
        return redirect(url_for("admin.program_edit", program_id=program.id))
    title = program.title
    _remove_old_file(program.cover_path)
    db.session.delete(program)
    db.session.commit()
    log_event("program", f"Передача удалена: {title}", "warning")
    flash("Передача удалена.", "success")
    return redirect(url_for("admin.programs"))


@admin_bp.route("/programs/<int:program_id>/move", methods=["POST"])
def program_move(program_id: int):
    program = _program_or_404(program_id)
    direction = request.form.get("direction", "up")
    rows = list(
        db.session.execute(select(Program).order_by(Program.position.asc(), Program.id.asc()))
        .scalars()
        .all()
    )
    index = next((position for position, row in enumerate(rows) if row.id == program.id), None)
    if index is not None:
        swap = index - 1 if direction == "up" else index + 1
        if 0 <= swap < len(rows):
            rows[index], rows[swap] = rows[swap], rows[index]
            # Normalise positions so duplicate/stale values cannot block reordering.
            for position, row in enumerate(rows):
                row.position = position
            db.session.commit()
            log_event("program", f"Изменён порядок передач: {program.title}")
    return redirect(url_for("admin.programs"))


@admin_bp.route("/programs/<int:program_id>/cover")
def program_cover(program_id: int):
    program = _program_or_404(program_id)
    if not program.cover_path:
        abort(404)
    path = Config.STORAGE_DIR / program.cover_path
    if not path.is_file():
        abort(404)
    return send_file(path, conditional=True, max_age=3600)


@admin_bp.route("/programs/<int:program_id>/items/add-url", methods=["POST"])
def program_add_url(program_id: int):
    program = _program_or_404(program_id)
    url = request.form.get("url", "")
    title = request.form.get("title", "")
    category = request.form.get("category", "")
    tags = request.form.get("tags", "")
    try:
        media = _create_media_from_url(url, title, category, tags)
        _add_item(program, media)
        flash(f"Видео «{media.title}» добавлено в передачу.", "success")
    except (UploadError, YtDlpError, SecurityError, FFmpegError, OSError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.program_edit", program_id=program.id))


@admin_bp.route("/programs/<int:program_id>/items/upload", methods=["POST"])
def program_upload(program_id: int):
    program = _program_or_404(program_id)
    file = request.files.get("video")
    if not file or not file.filename:
        flash("Выберите видеофайл.", "error")
        return redirect(url_for("admin.program_edit", program_id=program.id))
    try:
        media = _create_local_media(
            file,
            title=request.form.get("title", ""),
            category=request.form.get("category", ""),
            tags=request.form.get("tags", ""),
        )
        _add_item(program, media)
        flash(f"Видео «{media.title}» загружено и добавлено.", "success")
    except UploadError as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.program_edit", program_id=program.id))


@admin_bp.route("/programs/<int:program_id>/items/<int:item_id>/remove", methods=["POST"])
def program_item_remove(program_id: int, item_id: int):
    program = _program_or_404(program_id)
    item = db.session.get(ProgramItem, item_id)
    if item is None or item.program_id != program.id:
        abort(404)
    db.session.delete(item)
    db.session.commit()
    for position, row in enumerate(program.items):
        row.position = position
    db.session.commit()
    log_event("program", f"Из передачи «{program.title}» удалён элемент #{item_id}")
    flash("Видео удалено из передачи.", "success")
    return redirect(url_for("admin.program_edit", program_id=program.id))


@admin_bp.route("/programs/<int:program_id>/reorder", methods=["POST"])
def program_reorder(program_id: int):
    program = _program_or_404(program_id)
    payload = request.get_json(silent=True) or {}
    order = payload.get("order")
    if not isinstance(order, list):
        order = [part for part in request.form.get("order", "").split(",") if part.strip()]
    try:
        ids = [int(value) for value in order]
    except (TypeError, ValueError):
        return _json_error("Некорректный порядок элементов.")
    items = {item.id: item for item in program.items}
    if set(ids) != set(items.keys()):
        return _json_error("Список элементов изменился. Обновите страницу.")
    for position, item_id in enumerate(ids):
        items[item_id].position = position
    db.session.commit()
    log_event("program", f"Изменён порядок видео в передаче «{program.title}»")
    return _json_ok("Порядок сохранён.")


# ---------------------------------------------------------------------------
# Media library
# ---------------------------------------------------------------------------
@admin_bp.route("/media")
def media_library():
    query = clean_text(request.args.get("q"), 200)
    source = request.args.get("source", "")
    category = request.args.get("category", "")
    sort = request.args.get("sort", "new")
    statement = select(Media)
    if query:
        statement = statement.where(
            or_(Media.title.ilike(f"%{query}%"), Media.tags.ilike(f"%{query}%"))
        )
    if source in {"local", "url", "hls", "ytdlp"}:
        statement = statement.where(Media.source_type == source)
    if category:
        statement = statement.where(Media.category == category)
    if sort == "old":
        statement = statement.order_by(Media.created_at.asc())
    elif sort == "title":
        statement = statement.order_by(Media.title.asc())
    elif sort == "duration":
        statement = statement.order_by(Media.duration.desc().nullslast())
    else:
        statement = statement.order_by(Media.created_at.desc())
    rows = list(db.session.execute(statement.limit(500)).scalars().all())
    categories = [
        row[0]
        for row in db.session.execute(
            select(Media.category).where(Media.category != "").distinct().order_by(Media.category)
        ).all()
    ]
    usage = {media.id: media_usage_count(media.id) for media in rows}
    return render_template(
        "admin/media.html",
        active_page="media",
        media_rows=rows,
        categories=categories,
        usage=usage,
        q=query,
        source=source,
        category=category,
        sort=sort,
    )


@admin_bp.route("/media/add-url", methods=["POST"])
def media_add_url():
    try:
        media = _create_media_from_url(
            request.form.get("url", ""),
            request.form.get("title", ""),
            request.form.get("category", ""),
            request.form.get("tags", ""),
        )
        flash(f"Источник «{media.title}» добавлен в медиатеку.", "success")
    except (UploadError, YtDlpError, SecurityError, FFmpegError, OSError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.media_library"))


@admin_bp.route("/media/upload", methods=["POST"])
def media_upload():
    file = request.files.get("video")
    if not file or not file.filename:
        flash("Выберите видеофайл.", "error")
        return redirect(url_for("admin.media_library"))
    try:
        media = _create_local_media(
            file,
            title=request.form.get("title", ""),
            category=request.form.get("category", ""),
            tags=request.form.get("tags", ""),
        )
        flash(f"Видео «{media.title}» загружено.", "success")
    except UploadError as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.media_library"))


@admin_bp.route("/media/<int:media_id>/edit", methods=["GET", "POST"])
def media_edit(media_id: int):
    media = _media_or_404(media_id)
    if request.method == "POST":
        _set_media_fields(
            media,
            request.form.get("title", media.title),
            request.form.get("category", ""),
            request.form.get("tags", ""),
        )
        log_event("media", f"Метаданные источника обновлены: {media.title}")
        flash("Метаданные сохранены.", "success")
        return redirect(url_for("admin.media_library"))
    return render_template("admin/media_edit.html", active_page="media", media=media)


@admin_bp.route("/media/<int:media_id>/delete", methods=["POST"])
def media_delete(media_id: int):
    media = _media_or_404(media_id)
    usage = media_usage_count(media.id)
    if usage:
        flash(f"Источник используется в {usage} передаче(ах). Сначала удалите его из передач.", "error")
        return redirect(url_for("admin.media_library"))
    title = media.title
    if media.is_local:
        delete_stored(media.file_path)
    db.session.delete(media)
    db.session.commit()
    log_event("media", f"Источник удалён: {title}", "warning")
    flash("Источник удалён.", "success")
    return redirect(url_for("admin.media_library"))


@admin_bp.route("/media/<int:media_id>/check", methods=["POST"])
def media_check(media_id: int):
    media = _media_or_404(media_id)
    try:
        if media.source_type == "local":
            path = Config.STORAGE_DIR / (media.file_path or "")
            if not path.is_file():
                raise FFmpegError("Файл не найден.")
            probe = ffmpeg_manager.probe(str(path), remote=False)
        elif media.source_type == "ytdlp":
            stream = ytdlp_manager.extract(media.source_url, force=True)
            probe = ffmpeg_manager.probe(stream.url, headers=stream.headers, remote=True)
        else:
            url = validate_public_url(media.source_url or "")
            if media.source_type == "hls" or ".m3u8" in url.lower():
                validate_hls_playlist(url)
            probe = ffmpeg_manager.probe(url, remote=True)
        media.width = probe.width
        media.height = probe.height
        media.fps = probe.fps
        media.vcodec = probe.vcodec
        media.acodec = probe.acodec
        media.has_audio = probe.has_audio
        media.aspect = probe.aspect
        media.pix_fmt = probe.pix_fmt
        media.sample_rate = probe.sample_rate
        media.duration = probe.duration or media.duration
        media.status = "ok"
        media.last_error = None
        media.last_checked = datetime.now()
        db.session.commit()
        message = "Источник доступен."
        if _wants_json():
            return _json_ok(message, media=media.to_dict())
        flash(message, "success")
    except Exception as exc:
        media.status = "error"
        media.last_error = clean_text(str(exc), 1000)
        media.last_checked = datetime.now()
        db.session.commit()
        if _wants_json():
            return _json_error(str(exc), 400)
        flash(f"Источник недоступен: {exc}", "error")
    return redirect(url_for("admin.media_library"))


@admin_bp.route("/media/<int:media_id>/file")
def media_file(media_id: int):
    media = _media_or_404(media_id)
    if not media.is_local:
        abort(404)
    path = Config.STORAGE_DIR / (media.file_path or "")
    if not path.is_file():
        abort(404)
    return send_file(path, conditional=True, max_age=0)


@admin_bp.route("/media/<int:media_id>/preview")
def media_preview(media_id: int):
    media = _media_or_404(media_id)
    if media.is_local:
        return redirect(url_for("admin.media_file", media_id=media.id))
    try:
        if media.source_type == "ytdlp":
            stream = ytdlp_manager.extract(media.source_url)
            return redirect(stream.url)
        url = validate_public_url(media.source_url or "")
        return redirect(url)
    except Exception as exc:
        flash(f"Не удалось открыть предпросмотр: {exc}", "error")
        return redirect(url_for("admin.media_library"))


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------
@admin_bp.route("/schedule")
def schedule_page():
    rows = list(
        db.session.execute(
            select(ScheduleEntry)
            .order_by(ScheduleEntry.start_at.asc(), ScheduleEntry.id.asc())
        )
        .scalars()
        .all()
    )
    days = scheduler.upcoming(days=7)
    timeline = scheduler.timeline_for_day(datetime.now().date())
    programs = list(
        db.session.execute(select(Program).order_by(Program.position.asc(), Program.id.asc()))
        .scalars()
        .all()
    )
    return render_template(
        "admin/schedule.html",
        active_page="schedule",
        entries=rows,
        days=days,
        timeline=timeline,
        programs=programs,
        now=datetime.now(),
        mode=get_setting("channel_mode", "on"),
    )


def _parse_schedule_form() -> tuple[datetime | None, int, str]:
    raw = request.form.get("start_at", "").strip()
    parsed = None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    duration = parse_int(request.form.get("duration_minutes"), 30, 1, 1440)
    repeat = validate_repeat_days(",".join(request.form.getlist("repeat_days")))
    return parsed, duration, repeat


@admin_bp.route("/schedule/add", methods=["POST"])
def schedule_add():
    program = db.session.get(Program, parse_int(request.form.get("program_id"), 0))
    start_at, duration, repeat = _parse_schedule_form()
    if program is None:
        flash("Выберите передачу.", "error")
    elif start_at is None:
        flash("Укажите корректные дату и время начала.", "error")
    else:
        entry = ScheduleEntry(
            program_id=program.id,
            start_at=start_at,
            duration_minutes=duration,
            repeat_days=repeat,
            enabled=True,
        )
        db.session.add(entry)
        db.session.commit()
        log_event("schedule", f"Добавлено в расписание: {program.title} — {start_at:%d.%m.%Y %H:%M}")
        flash("Передача добавлена в расписание.", "success")
    return redirect(url_for("admin.schedule_page"))


@admin_bp.route("/schedule/<int:entry_id>/update", methods=["POST"])
def schedule_update(entry_id: int):
    entry = db.session.get(ScheduleEntry, entry_id)
    if entry is None:
        abort(404)
    start_at, duration, repeat = _parse_schedule_form()
    if start_at is None:
        flash("Укажите корректные дату и время.", "error")
        return redirect(url_for("admin.schedule_page"))
    program = db.session.get(Program, parse_int(request.form.get("program_id"), entry.program_id))
    if program is None:
        flash("Передача не найдена.", "error")
        return redirect(url_for("admin.schedule_page"))
    entry.program_id = program.id
    entry.start_at = start_at
    entry.duration_minutes = duration
    entry.repeat_days = repeat
    entry.enabled = parse_bool(request.form.get("enabled"), True)
    db.session.commit()
    log_event("schedule", f"Расписание обновлено: {program.title} — {start_at:%d.%m.%Y %H:%M}")
    flash("Расписание обновлено.", "success")
    return redirect(url_for("admin.schedule_page"))


@admin_bp.route("/schedule/<int:entry_id>/delete", methods=["POST"])
def schedule_delete(entry_id: int):
    entry = db.session.get(ScheduleEntry, entry_id)
    if entry is None:
        abort(404)
    title = entry.program.title if entry.program else str(entry.program_id)
    db.session.delete(entry)
    db.session.commit()
    log_event("schedule", f"Удалено из расписания: {title}", "warning")
    flash("Запись расписания удалена.", "success")
    return redirect(url_for("admin.schedule_page"))


@admin_bp.route("/schedule/<int:entry_id>/toggle", methods=["POST"])
def schedule_toggle(entry_id: int):
    entry = db.session.get(ScheduleEntry, entry_id)
    if entry is None:
        abort(404)
    entry.enabled = not entry.enabled
    db.session.commit()
    log_event("schedule", f"Расписание {'включено' if entry.enabled else 'выключено'}: #{entry.id}")
    return redirect(url_for("admin.schedule_page"))


# ---------------------------------------------------------------------------
# Channel settings
# ---------------------------------------------------------------------------
@admin_bp.route("/settings")
def settings_page():
    return render_template(
        "admin/settings.html",
        active_page="settings",
        settings=dict(
            channel_name=get_setting("channel_name", "SIMPLE"),
            channel_id=get_setting("channel_id", "simple"),
            channel_description=get_setting("channel_description", ""),
            public_base_url=get_setting("public_base_url", ""),
            channel_mode=get_setting("channel_mode", "on"),
            logo_enabled=get_bool_setting("logo_enabled", True),
            logo_file=get_setting("logo_file", ""),
            maintenance_image=get_setting("maintenance_image", ""),
            maintenance_video=get_setting("maintenance_video", ""),
            maintenance_music=get_setting("maintenance_music", ""),
            maintenance_music_enabled=get_bool_setting("maintenance_music_enabled", True),
            maintenance_message=get_setting("maintenance_message", ""),
            standby_text=get_setting("standby_text", ""),
            output_width=get_int_setting("output_width", 1280),
            output_height=get_int_setting("output_height", 720),
            output_fps=get_int_setting("output_fps", 30),
            video_bitrate=get_setting("video_bitrate", "2200k"),
            audio_bitrate=get_setting("audio_bitrate", "128k"),
            hls_time=get_int_setting("hls_time", 2),
            hls_list_size=get_int_setting("hls_list_size", 10),
            respect_schedule=get_bool_setting("respect_schedule", True),
            max_retries=get_int_setting("max_retries", 2),
            default_program_minutes=get_int_setting("default_program_minutes", 30),
        ),
        binaries=ffmpeg_manager.check_binaries(),
        base_url=public_base_url(request),
        engine_status=_engine().status() if _engine() else {},
    )


@admin_bp.route("/settings/general", methods=["POST"])
def settings_general():
    values: dict[str, str] = {}
    values["channel_name"] = clean_text(request.form.get("channel_name"), 80, "SIMPLE") or "SIMPLE"
    channel_id = re.sub(r"[^a-zA-Z0-9_-]", "", request.form.get("channel_id", "simple"))[:40]
    values["channel_id"] = channel_id or "simple"
    values["channel_description"] = clean_text(request.form.get("channel_description"), 1000)
    base_url = (request.form.get("public_base_url") or "").strip().rstrip("/")
    if base_url:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            flash("Публичный адрес должен начинаться с http:// или https://.", "error")
            return redirect(url_for("admin.settings_page"))
    values["public_base_url"] = base_url
    values["output_width"] = str(parse_int(request.form.get("output_width"), 1280, 320, 1920))
    values["output_height"] = str(parse_int(request.form.get("output_height"), 720, 180, 1080))
    values["output_fps"] = str(parse_int(request.form.get("output_fps"), 30, 15, 60))
    video_bitrate = request.form.get("video_bitrate", "2200k").strip()
    values["video_bitrate"] = video_bitrate if re.fullmatch(r"\d{3,5}k", video_bitrate) else "2200k"
    audio_bitrate = request.form.get("audio_bitrate", "128k").strip()
    values["audio_bitrate"] = audio_bitrate if re.fullmatch(r"\d{2,4}k", audio_bitrate) else "128k"
    values["hls_time"] = str(parse_int(request.form.get("hls_time"), 2, 1, 10))
    values["hls_list_size"] = str(parse_int(request.form.get("hls_list_size"), 10, 3, 30))
    values["respect_schedule"] = "1" if parse_bool(request.form.get("respect_schedule"), False) else "0"
    values["max_retries"] = str(parse_int(request.form.get("max_retries"), 2, 0, 5))
    values["default_program_minutes"] = str(
        parse_int(request.form.get("default_program_minutes"), 30, 1, 1440)
    )
    values["standby_text"] = clean_text(request.form.get("standby_text"), 200)
    values["maintenance_message"] = clean_text(request.form.get("maintenance_message"), 500)
    values["logo_enabled"] = "1" if parse_bool(request.form.get("logo_enabled"), False) else "0"
    set_settings(values)
    log_event("admin", f"{session.get('admin_username')}: обновлены настройки канала")
    flash("Настройки канала сохранены.", "success")
    return redirect(url_for("admin.settings_page"))


@admin_bp.route("/settings/mode", methods=["POST"])
def settings_mode():
    mode = request.form.get("mode", "")
    if mode not in {"on", "off", "maintenance"}:
        flash("Неизвестный режим канала.", "error")
        return redirect(url_for("admin.settings_page"))
    set_setting("channel_mode", mode)
    labels = {"on": "эфир включён", "off": "эфир выключен", "maintenance": "профилактика"}
    log_event("channel", f"{session.get('admin_username')}: {labels[mode]}", "warning")
    flash(f"Режим канала: {labels[mode]}.", "success")
    return redirect(_safe_next(request.form.get("next")))


@admin_bp.route("/settings/logo", methods=["POST"])
def settings_logo():
    file = request.files.get("logo")
    if not file or not file.filename:
        flash("Выберите PNG-файл логотипа.", "error")
        return redirect(url_for("admin.settings_page"))
    try:
        _, relative, width, height = save_logo(file)
        _save_setting_file("logo_file", relative)
        set_setting("logo_enabled", "1")
        log_event("settings", f"Загружен логотип {width}×{height}", "warning")
        flash("Логотип загружен. Размер 342×74 подтверждён.", "success")
    except UploadError as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.settings_page"))


@admin_bp.route("/settings/logo/delete", methods=["POST"])
def settings_logo_delete():
    old = get_setting("logo_file", "")
    set_setting("logo_file", "")
    _remove_old_file(old)
    log_event("settings", f"{session.get('admin_username')}: логотип удалён", "warning")
    flash("Логотип удалён.", "success")
    return redirect(url_for("admin.settings_page"))


@admin_bp.route("/settings/logo/toggle", methods=["POST"])
def settings_logo_toggle():
    enabled = not get_bool_setting("logo_enabled", True)
    set_setting("logo_enabled", "1" if enabled else "0")
    log_event("settings", f"Логотип {'включён' if enabled else 'выключен'}")
    return redirect(url_for("admin.settings_page"))


@admin_bp.route("/settings/maintenance/<asset>", methods=["POST"])
def settings_maintenance_upload(asset: str):
    if asset not in {"image", "video", "music"}:
        abort(404)
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Выберите файл.", "error")
        return redirect(url_for("admin.settings_page"))
    try:
        if asset == "image":
            _, relative, width, height = save_image(file, "maintenance")
            detail = f"{width}×{height}"
        elif asset == "video":
            _, relative, probe, _ = save_video(file, "maintenance")
            detail = f"{probe.width}×{probe.height}"
        else:
            _, relative, probe, _ = save_audio(file, "maintenance")
            detail = f"{probe.sample_rate} Гц"
        _save_setting_file(f"maintenance_{asset}", relative)
        log_event("settings", f"Загружен файл профилактики ({asset}, {detail})", "warning")
        flash("Файл профилактики загружен.", "success")
    except (UploadError, FFmpegError, OSError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.settings_page"))


@admin_bp.route("/settings/maintenance/<asset>/delete", methods=["POST"])
def settings_maintenance_delete(asset: str):
    if asset not in {"image", "video", "music"}:
        abort(404)
    old = get_setting(f"maintenance_{asset}", "")
    set_setting(f"maintenance_{asset}", "")
    _remove_old_file(old)
    log_event("settings", f"Удалён файл профилактики ({asset})", "warning")
    flash("Файл удалён.", "success")
    return redirect(url_for("admin.settings_page"))


@admin_bp.route("/settings/maintenance/music/toggle", methods=["POST"])
def settings_maintenance_music_toggle():
    enabled = not get_bool_setting("maintenance_music_enabled", True)
    set_setting("maintenance_music_enabled", "1" if enabled else "0")
    log_event("settings", f"Музыка профилактики {'включена' if enabled else 'выключена'}")
    return redirect(url_for("admin.settings_page"))


@admin_bp.route("/settings/password", methods=["POST"])
def settings_password():
    user = current_user()
    if user is None:
        return redirect(url_for("admin.login"))
    error = change_password(
        user,
        request.form.get("old_password", ""),
        request.form.get("new_password", ""),
    )
    if error:
        flash(error, "error")
    else:
        log_event("auth", f"{user.username}: пароль изменён", "warning")
        flash("Пароль изменён.", "success")
    return redirect(url_for("admin.settings_page"))


@admin_bp.route("/settings/engine/restart", methods=["POST"])
def settings_engine_restart():
    engine = _engine()
    if engine is None:
        flash("Эфирный движок не инициализирован.", "error")
    else:
        engine.stop()
        engine.start()
        log_event("admin", f"{session.get('admin_username')}: эфирный движок перезапущен", "warning")
        flash("Эфирный движок перезапущен.", "success")
    return redirect(url_for("admin.settings_page"))


# ---------------------------------------------------------------------------
# Ads
# ---------------------------------------------------------------------------
@admin_bp.route("/ads")
def ads_page():
    rows = list(db.session.execute(select(Ad).order_by(Ad.created_at.desc())).scalars().all())
    return render_template(
        "admin/ads.html",
        active_page="ads",
        ads=rows,
        settings=dict(
            ads_enabled=get_bool_setting("ads_enabled", True),
            ad_after_video_probability=get_int_setting("ad_after_video_probability", 0),
            ad_after_program=get_bool_setting("ad_after_program", True),
            ad_block_size=get_int_setting("ad_block_size", 1),
        ),
    )


@admin_bp.route("/ads/upload", methods=["POST"])
def ads_upload():
    file = request.files.get("video")
    if not file or not file.filename:
        flash("Выберите рекламный ролик.", "error")
        return redirect(url_for("admin.ads_page"))
    try:
        _, relative, probe, _ = save_ad(file)
        ad = Ad(
            title=clean_text(request.form.get("title") or suggested_title(file.filename), 300, "Реклама"),
            file_path=relative,
            duration=probe.duration,
            width=probe.width,
            height=probe.height,
            enabled=True,
        )
        db.session.add(ad)
        db.session.commit()
        log_event("ads", f"Загружен рекламный ролик: {ad.title}")
        flash("Рекламный ролик загружен.", "success")
    except UploadError as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.ads_page"))


@admin_bp.route("/ads/<int:ad_id>/toggle", methods=["POST"])
def ads_toggle(ad_id: int):
    ad = db.session.get(Ad, ad_id)
    if ad is None:
        abort(404)
    ad.enabled = not ad.enabled
    db.session.commit()
    return redirect(url_for("admin.ads_page"))


@admin_bp.route("/ads/<int:ad_id>/delete", methods=["POST"])
def ads_delete(ad_id: int):
    ad = db.session.get(Ad, ad_id)
    if ad is None:
        abort(404)
    title = ad.title
    delete_stored(ad.file_path)
    db.session.delete(ad)
    db.session.commit()
    log_event("ads", f"Удалён рекламный ролик: {title}", "warning")
    flash("Рекламный ролик удалён.", "success")
    return redirect(url_for("admin.ads_page"))


@admin_bp.route("/ads/<int:ad_id>/preview")
def ads_preview(ad_id: int):
    ad = db.session.get(Ad, ad_id)
    if ad is None:
        abort(404)
    path = ad.absolute_path
    if not path.is_file():
        abort(404)
    return send_file(path, conditional=True)


@admin_bp.route("/ads/settings", methods=["POST"])
def ads_settings():
    values = {
        "ads_enabled": "1" if parse_bool(request.form.get("ads_enabled"), False) else "0",
        "ad_after_video_probability": str(
            parse_int(request.form.get("ad_after_video_probability"), 0, 0, 100)
        ),
        "ad_after_program": "1" if parse_bool(request.form.get("ad_after_program"), False) else "0",
        "ad_block_size": str(parse_int(request.form.get("ad_block_size"), 1, 1, 5)),
    }
    set_settings(values)
    log_event("admin", f"{session.get('admin_username')}: обновлены настройки рекламы")
    flash("Настройки рекламы сохранены.", "success")
    return redirect(url_for("admin.ads_page"))


# ---------------------------------------------------------------------------
# Bumpers
# ---------------------------------------------------------------------------
@admin_bp.route("/bumpers")
def bumpers_page():
    rows = list(db.session.execute(select(Bumper).order_by(Bumper.created_at.desc())).scalars().all())
    return render_template(
        "admin/bumpers.html",
        active_page="bumpers",
        bumpers=rows,
        settings=dict(
            bumpers_enabled=get_bool_setting("bumpers_enabled", True),
            bumper_probability=get_int_setting("bumper_probability", 100),
        ),
    )


@admin_bp.route("/bumpers/upload", methods=["POST"])
def bumpers_upload():
    file = request.files.get("video")
    if not file or not file.filename:
        flash("Выберите видео бампера.", "error")
        return redirect(url_for("admin.bumpers_page"))
    try:
        _, relative, probe, _ = save_bumper(file)
        bumper = Bumper(
            title=clean_text(request.form.get("title") or suggested_title(file.filename), 300, "Бампер"),
            file_path=relative,
            duration=probe.duration,
            width=probe.width,
            height=probe.height,
            enabled=True,
            probability=parse_int(request.form.get("probability"), 100, 1, 100),
        )
        db.session.add(bumper)
        db.session.commit()
        log_event("bumpers", f"Загружен бампер: {bumper.title}")
        flash("Бампер загружен.", "success")
    except UploadError as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.bumpers_page"))


@admin_bp.route("/bumpers/<int:bumper_id>/toggle", methods=["POST"])
def bumpers_toggle(bumper_id: int):
    bumper = db.session.get(Bumper, bumper_id)
    if bumper is None:
        abort(404)
    bumper.enabled = not bumper.enabled
    db.session.commit()
    return redirect(url_for("admin.bumpers_page"))


@admin_bp.route("/bumpers/<int:bumper_id>/probability", methods=["POST"])
def bumpers_probability(bumper_id: int):
    bumper = db.session.get(Bumper, bumper_id)
    if bumper is None:
        abort(404)
    bumper.probability = parse_int(request.form.get("probability"), 100, 1, 100)
    db.session.commit()
    return redirect(url_for("admin.bumpers_page"))


@admin_bp.route("/bumpers/<int:bumper_id>/delete", methods=["POST"])
def bumpers_delete(bumper_id: int):
    bumper = db.session.get(Bumper, bumper_id)
    if bumper is None:
        abort(404)
    title = bumper.title
    delete_stored(bumper.file_path)
    db.session.delete(bumper)
    db.session.commit()
    log_event("bumpers", f"Удалён бампер: {title}", "warning")
    flash("Бампер удалён.", "success")
    return redirect(url_for("admin.bumpers_page"))


@admin_bp.route("/bumpers/<int:bumper_id>/preview")
def bumpers_preview(bumper_id: int):
    bumper = db.session.get(Bumper, bumper_id)
    if bumper is None:
        abort(404)
    path = bumper.absolute_path
    if not path.is_file():
        abort(404)
    return send_file(path, conditional=True)


@admin_bp.route("/bumpers/settings", methods=["POST"])
def bumpers_settings():
    set_settings(
        {
            "bumpers_enabled": "1" if parse_bool(request.form.get("bumpers_enabled"), False) else "0",
            "bumper_probability": str(parse_int(request.form.get("bumper_probability"), 100, 0, 100)),
        }
    )
    log_event("admin", f"{session.get('admin_username')}: обновлены настройки бамперов")
    flash("Настройки бамперов сохранены.", "success")
    return redirect(url_for("admin.bumpers_page"))


# ---------------------------------------------------------------------------
# Broadcast journal
# ---------------------------------------------------------------------------
@admin_bp.route("/logs")
def logs_page():
    page = max(1, parse_int(request.args.get("page"), 1))
    per_page = 100
    q = clean_text(request.args.get("q"), 200)
    level = request.args.get("level", "")
    category = request.args.get("category", "")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    conditions = []
    if q:
        conditions.append(EventLog.message.ilike(f"%{q}%"))
    if level in {"debug", "info", "warning", "error", "critical"}:
        conditions.append(EventLog.level == level)
    if category:
        conditions.append(EventLog.category == category)
    try:
        if date_from:
            conditions.append(EventLog.created_at >= datetime.strptime(date_from, "%Y-%m-%d"))
        if date_to:
            conditions.append(
                EventLog.created_at < datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            )
    except ValueError:
        flash("Некорректная дата фильтра.", "error")
    statement = select(EventLog)
    count_statement = select(func.count(EventLog.id))
    for condition in conditions:
        statement = statement.where(condition)
        count_statement = count_statement.where(condition)
    total = int(db.session.execute(count_statement).scalar() or 0)
    rows = list(
        db.session.execute(
            statement.order_by(EventLog.created_at.desc(), EventLog.id.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        .scalars()
        .all()
    )
    categories = [
        row[0]
        for row in db.session.execute(
            select(EventLog.category).distinct().order_by(EventLog.category)
        ).all()
    ]
    pages = max(1, (total + per_page - 1) // per_page)
    return render_template(
        "admin/logs.html",
        active_page="logs",
        events=rows,
        categories=categories,
        page=page,
        pages=pages,
        total=total,
        q=q,
        level=level,
        category=category,
        date_from=date_from,
        date_to=date_to,
    )


@admin_bp.route("/logs/clear", methods=["POST"])
def logs_clear():
    days = parse_int(request.form.get("days"), 0, 0, 3650)
    if days > 0:
        cutoff = datetime.now() - timedelta(days=days)
        result = db.session.execute(select(EventLog).where(EventLog.created_at < cutoff))
        count = 0
        for row in result.scalars().all():
            db.session.delete(row)
            count += 1
        db.session.commit()
        log_event("admin", f"{session.get('admin_username')}: очищено старых событий: {count}", "warning")
        flash(f"Удалено событий: {count}.", "success")
    else:
        db.session.execute(EventLog.__table__.delete())
        db.session.commit()
        log_event("admin", f"{session.get('admin_username')}: журнал очищен полностью", "warning")
        flash("Журнал очищен.", "success")
    return redirect(url_for("admin.logs_page"))


# ---------------------------------------------------------------------------
# Monitoring and diagnostics
# ---------------------------------------------------------------------------
@admin_bp.route("/monitoring")
def monitoring_page():
    return render_template(
        "admin/monitoring.html",
        active_page="monitoring",
        metrics=system_metrics(),
        status=_engine().status() if _engine() else {},
        base_url=public_base_url(request),
    )


@admin_bp.route("/api/monitoring")
def api_monitoring():
    engine = _engine()
    return jsonify(
        {
            "ok": True,
            "metrics": system_metrics(),
            "engine": engine.status() if engine else {},
            "mode": get_setting("channel_mode", "on"),
            "viewers": stream_viewers(),
        }
    )


def stream_viewers() -> int:
    from stream_manager import stream_manager

    return stream_manager.viewers.count()


@admin_bp.route("/test")
def test_page():
    return render_template("admin/test.html", active_page="test", base_url=public_base_url(request))


@admin_bp.route("/test/run", methods=["POST"])
def test_run():
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, required: bool = True) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "required": required})

    binaries = ffmpeg_manager.check_binaries()
    add("FFmpeg", binaries["ffmpeg"]["ok"], binaries["ffmpeg"]["version"])
    add("FFprobe", binaries["ffprobe"]["ok"], binaries["ffprobe"]["version"])
    add("yt-dlp", binaries["ytdlp"]["ok"], binaries["ytdlp"]["version"])

    try:
        Config.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        probe_file = Config.STORAGE_DIR / "tmp" / "write_test.txt"
        probe_file.parent.mkdir(parents=True, exist_ok=True)
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink()
        add("Каталог хранения", True, str(Config.STORAGE_DIR))
    except OSError as exc:
        add("Каталог хранения", False, str(exc))

    try:
        media_count = int(db.session.execute(select(func.count(Media.id))).scalar() or 0)
        program_count = int(db.session.execute(select(func.count(Program.id))).scalar() or 0)
        add("База данных", True, f"Медиа: {media_count}, передач: {program_count}")
        add(
            "Источники видео",
            media_count > 0,
            "Добавьте хотя бы один источник в медиатеку." if media_count == 0 else f"Доступно источников: {media_count}",
            required=False,
        )
        add(
            "Передачи",
            program_count > 0,
            "Создайте передачу и добавьте в неё видео." if program_count == 0 else f"Передач: {program_count}",
            required=False,
        )
    except Exception as exc:
        add("База данных", False, str(exc))

    logo = get_setting("logo_file", "")
    if logo:
        path = Config.STORAGE_DIR / logo
        try:
            from PIL import Image

            with Image.open(path) as image:
                size = image.size
            add("Логотип", size == Config.LOGO_SIZE, f"{size[0]}×{size[1]} px")
        except Exception as exc:
            add("Логотип", False, f"Файл логотипа не читается: {exc}")
    else:
        add("Логотип", True, "Логотип не загружен (необязательно).", required=False)

    bumpers = int(db.session.execute(select(func.count(Bumper.id))).scalar() or 0)
    ads = int(db.session.execute(select(func.count(Ad.id))).scalar() or 0)
    add("Бамперы", True, f"Загружено: {bumpers}", required=False)
    add("Реклама", True, f"Загружено: {ads}", required=False)

    try:
        epg_xml = build_epg_xml(public_base_url(request), days=2)
        from xml.etree import ElementTree as ET

        root = ET.fromstring(epg_xml)
        add("EPG XML", root.tag == "tv", f"Элементов programme: {len(root.findall('programme'))}")
        gz = build_epg_gzip(public_base_url(request), days=1)
        add("EPG GZIP", len(gz) > 20, f"Размер: {human_size(len(gz))}")
    except Exception as exc:
        add("EPG XML", False, str(exc))
        add("EPG GZIP", False, str(exc))

    try:
        m3u = build_m3u(public_base_url(request))
        add("M3U", "#EXTM3U" in m3u and "/live/index.m3u8" in m3u, "Плейлист сформирован.")
    except Exception as exc:
        add("M3U", False, str(exc))

    test_dir = Config.HLS_DIR / "_test"
    try:
        result = ffmpeg_manager.make_test_hls(test_dir, duration=3)
        add(
            "Генерация HLS",
            result["ok"],
            "Тестовый HLS создан." if result["ok"] else (result["stderr"] or "FFmpeg не создал плейлист."),
        )
    except Exception as exc:
        add("Генерация HLS", False, str(exc))
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)

    engine = _engine()
    add(
        "Эфирный движок",
        bool(engine and engine.is_running()),
        "Движок запущен." if engine and engine.is_running() else "Движок не запущен.",
        required=False,
    )
    add(
        "Актуальный HLS",
        stream_viewers() >= 0 and bool(engine and engine.status().get("hls_ready")),
        "Плейлист актуален." if engine and engine.status().get("hls_ready") else "HLS ещё не сформирован.",
        required=False,
    )
    required_ok = all(item["ok"] for item in checks if item["required"])
    return jsonify({"ok": required_ok, "checks": checks})
