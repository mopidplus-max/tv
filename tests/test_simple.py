from __future__ import annotations

import re
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

import pytest
from sqlalchemy import select

import scheduler
from config import Config
from database import (
    Ad,
    Bumper,
    Media,
    Program,
    ProgramItem,
    ScheduleEntry,
    db,
    set_setting,
)
from epg import build_epg_gzip, build_epg_xml
from playlist import build_m3u
from security import SecurityError, safe_join, sanitize_tags, validate_public_url


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token not found"
    return match.group(1)


def _login(client):
    url = f"{Config.ADMIN_PATH}login"
    page = client.get(url)
    assert page.status_code == 200
    token = _csrf(page.get_data(as_text=True))
    response = client.post(
        url,
        data={
            "username": Config.ADMIN_USERNAME,
            "password": Config.ADMIN_PASSWORD,
            "csrf_token": token,
        },
    )
    assert response.status_code == 302
    return response


def test_public_pages_and_hidden_admin(client):
    for path in ("/", "/tv", "/playlist.m3u", "/epg.xml", "/epg.xml.gz", "/robots.txt", "/healthz", "/favicon.svg"):
        response = client.get(path)
        assert response.status_code == 200, path

    home = client.get("/").get_data(as_text=True)
    assert Config.ADMIN_PATH not in home
    assert "admin" not in home.lower()
    robots = client.get("/robots.txt").get_data(as_text=True)
    assert "Disallow: /" in robots


def test_login_and_csrf(client):
    _login(client)
    dashboard = client.get(Config.ADMIN_PATH)
    assert dashboard.status_code == 200
    assert "Панель канала" in dashboard.get_data(as_text=True)
    # POST without a token must be rejected.
    assert client.post(f"{Config.ADMIN_PATH}logout").status_code == 400


def test_admin_pages_render(client):
    _login(client)
    pages = [
        "",
        "preview",
        "programs",
        "programs/new",
        "media",
        "schedule",
        "settings",
        "ads",
        "bumpers",
        "logs",
        "monitoring",
        "test",
    ]
    for page in pages:
        response = client.get(Config.ADMIN_PATH + page)
        assert response.status_code == 200, page
        assert "SIMPLE" in response.get_data(as_text=True)


def test_schedule_rotation_and_epg(app):
    with app.app_context():
        media = Media(title="Тестовое видео", source_type="url", source_url="https://example.com/v.mp4", duration=120, status="ok")
        program = Program(title="Тестовая передача", description="Описание", position=1, enabled=True)
        db.session.add_all([media, program])
        db.session.commit()
        item = ProgramItem(program_id=program.id, media_id=media.id, position=0)
        entry = ScheduleEntry(
            program_id=program.id,
            start_at=datetime.now() - timedelta(minutes=2),
            duration_minutes=30,
            repeat_days="",
            enabled=True,
        )
        db.session.add_all([item, entry])
        db.session.commit()

        active = scheduler.active_occurrence()
        assert active is not None
        assert active.program_id == program.id
        assert active.title == "Тестовая передача"

        xml = build_epg_xml("http://127.0.0.1:5000", days=2)
        root = ET.fromstring(xml)
        assert root.tag == "tv"
        assert root.findall("programme")

        gz = build_epg_gzip("http://127.0.0.1:5000", days=1)
        assert gz[:2] == b"\x1f\x8b"

        m3u = build_m3u("http://127.0.0.1:5000")
        assert m3u.startswith("#EXTM3U")
        assert "http://127.0.0.1:5000/live/index.m3u8" in m3u


def test_media_models_and_counts(app):
    with app.app_context():
        media = Media(title="Файл", source_type="local", file_path="videos/a.mp4", duration=61.5, width=1280, height=720, status="ok")
        db.session.add(media)
        db.session.commit()
        assert media.is_local
        assert media.resolution_label == "1280×720"
        assert media.duration_label == "1:01"
        assert media.to_dict()["source_label"] == "Файл"

        bumper = Bumper(title="Бампер", file_path="bumpers/b.mp4", duration=3, probability=50)
        ad = Ad(title="Реклама", file_path="ads/c.mp4", duration=10)
        db.session.add_all([bumper, ad])
        db.session.commit()
        assert bumper.to_dict()["probability"] == 50
        assert ad.to_dict()["title"] == "Реклама"


def test_security_helpers(tmp_path):
    with pytest.raises(SecurityError):
        validate_public_url("http://127.0.0.1/private", resolve=False)
    with pytest.raises(SecurityError):
        validate_public_url("http://localhost/test", resolve=False)
    with pytest.raises(SecurityError):
        validate_public_url("ftp://example.com/video", resolve=False)
    assert validate_public_url("https://example.com/video", resolve=False).startswith("https://")
    assert sanitize_tags("эфир, вечер, эфир, bad<script>") == "эфир, вечер"
    with pytest.raises(SecurityError):
        safe_join(tmp_path, "../escape.txt")
    assert safe_join(tmp_path, "inside.txt").parent == tmp_path.resolve()


def test_public_mode_and_error_pages(client, app):
    with app.app_context():
        set_setting("channel_mode", "off")
    response = client.get("/")
    assert response.status_code == 200
    assert "Канал временно не работает" in response.get_data(as_text=True)

    with app.app_context():
        set_setting("channel_mode", "maintenance")
        set_setting("maintenance_message", "Плановые технические работы")
    response = client.get("/")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "Скоро вернёмся в эфир" in text
    assert "Плановые технические работы" in text

    with app.app_context():
        set_setting("channel_mode", "on")

    response = client.get("/this-page-does-not-exist")
    assert response.status_code == 404
    text = response.get_data(as_text=True)
    assert "404" in text
    assert "Страница не найдена" in text


def test_public_schedule_api_rejects_bad_days(client):
    response = client.get("/api/public/schedule?days=not-a-number")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert isinstance(payload["items"], list)


def test_admin_edit_pages_render(client, app):
    _login(client)
    with app.app_context():
        media = Media(
            title="Источник для правки",
            source_type="local",
            file_path="videos/edit.mp4",
            duration=90,
            status="ok",
        )
        program = Program(title="Передача для правки", position=1, enabled=True)
        db.session.add_all([media, program])
        db.session.commit()
        media_id, program_id = media.id, program.id

    for path in (f"media/{media_id}/edit", f"programs/{program_id}/edit"):
        response = client.get(Config.ADMIN_PATH + path)
        assert response.status_code == 200, path
        assert "SIMPLE" in response.get_data(as_text=True)


def test_schedule_rejects_malformed_input(client, app):
    _login(client)
    with app.app_context():
        program = Program(title="Расписание", position=1, enabled=True)
        db.session.add(program)
        db.session.commit()
        program_id = program.id

    page = client.get(Config.ADMIN_PATH + "schedule")
    token = _csrf(page.get_data(as_text=True))
    response = client.post(
        Config.ADMIN_PATH + "schedule/add",
        data={
            "csrf_token": token,
            "program_id": str(program_id),
            "start_at": "not-a-date",
            "duration_minutes": "30",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Укажите корректные дату и время начала" in response.get_data(as_text=True)

    with app.app_context():
        entries = list(db.session.execute(select(ScheduleEntry)).scalars().all())
        assert entries == []


def test_program_move_normalises_duplicate_positions(client, app):
    _login(client)
    with app.app_context():
        first = Program(title="A", position=0, enabled=True)
        second = Program(title="B", position=0, enabled=True)
        third = Program(title="C", position=0, enabled=True)
        db.session.add_all([first, second, third])
        db.session.commit()
        ids = [first.id, second.id, third.id]

    page = client.get(Config.ADMIN_PATH + "programs")
    token = _csrf(page.get_data(as_text=True))
    response = client.post(
        Config.ADMIN_PATH + f"programs/{ids[2]}/move",
        data={"csrf_token": token, "direction": "up"},
    )
    assert response.status_code == 302

    with app.app_context():
        rows = list(
            db.session.execute(select(Program).order_by(Program.position.asc(), Program.id.asc()))
            .scalars()
            .all()
        )
        assert [row.id for row in rows] == [ids[0], ids[2], ids[1]]
        assert [row.position for row in rows] == [0, 1, 2]


def test_audio_only_probe_and_save(app):
    import wave
    from io import BytesIO

    from werkzeug.datastructures import FileStorage

    from ffmpeg_manager import FFmpegError, ffmpeg_manager
    from uploads import save_audio

    if not ffmpeg_manager.check_binaries()["ffprobe"]["ok"]:
        pytest.skip("ffprobe is not available")

    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * 8000)
    buffer.seek(0)

    with app.app_context():
        Config.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        temp = Config.STORAGE_DIR / "tone.wav"
        temp.write_bytes(buffer.getvalue())

        probe = ffmpeg_manager.probe(str(temp), remote=False, require_video=False)
        assert probe.has_audio is True
        assert probe.has_video is False
        assert probe.sample_rate == 8000

        with pytest.raises(FFmpegError):
            ffmpeg_manager.probe(str(temp), remote=False)

        buffer.seek(0)
        storage = FileStorage(buffer, filename="tone.wav", content_type="audio/wav")
        final, relative, saved_probe, size = save_audio(storage)
        assert final.is_file()
        assert relative.startswith("maintenance/")
        assert saved_probe.has_audio is True
        assert size > 0
