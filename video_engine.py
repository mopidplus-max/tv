"""The single shared broadcast engine.

Only one instance of this engine may run per SIMPLE process. It owns the
current FFmpeg process and writes to the shared HLS directory; viewers never
spawn their own FFmpeg.
"""
from __future__ import annotations

import hashlib
import random
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from flask import Flask
from sqlalchemy import select

import scheduler
from config import Config
from database import (
    Ad,
    Bumper,
    Media,
    Program,
    ProgramItem,
    db,
    enabled_programs,
    get_bool_setting,
    get_int_setting,
    get_playback_state,
    get_setting,
    log_event,
    program_by_id,
    save_playback_state,
)
from ffmpeg_manager import FFmpegError, FFmpegProcess, ProbeResult, StreamSpec, ffmpeg_manager
from security import SecurityError, safe_join, validate_hls_playlist, validate_public_url
from stream_manager import stream_manager
from utils import clean_text, get_logger, now_local
from ytdlp_manager import YtDlpError, ytdlp_manager

logger = get_logger("engine")


class BroadcastEngine:
    def __init__(self, app: Flask) -> None:
        self.app = app
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._current_process: FFmpegProcess | None = None
        self._paused = False
        self._skip_requested = False
        self._restart_requested = False
        self._next_program_requested = False
        self._override_item_id: int | None = None
        self._manual_until: datetime | None = None
        self._last_bumper_id: int | None = None
        self._last_ad_id: int | None = None
        self._mode_value = "on"
        self._mode_checked = 0.0
        self._last_mode: str | None = None
        self._started_at = time.time()
        self._rng = random.Random()
        self._status: dict[str, Any] = {
            "state": "idle",
            "phase": "idle",
            "mode": "on",
            "program_id": None,
            "program_title": "",
            "program_description": "",
            "item_id": None,
            "media_id": None,
            "media_title": "",
            "item_index": 0,
            "items_total": 0,
            "item_started_at": None,
            "item_duration": None,
            "program_started_at": None,
            "program_ends_at": None,
            "next_program_title": "",
            "paused": False,
            "ffmpeg_running": False,
            "ffmpeg_pid": None,
            "progress": 0.0,
            "program_progress": 0.0,
            "elapsed": 0.0,
            "remaining": None,
            "last_error": "",
            "updated_at": now_local().isoformat(),
        }

    # ------------------------------------------------------------------
    # Lifecycle and public control
    # ------------------------------------------------------------------
    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="simple-broadcast-engine", daemon=True
            )
            self._thread.start()
            return True

    def stop(self) -> None:
        self._stop.set()
        self._terminate_current()
        thread = self._thread
        if thread:
            thread.join(timeout=12)
        self._set_status(state="idle", phase="idle", paused=False, ffmpeg_running=False)
        logger.info("Эфирный движок остановлен")

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def pause(self) -> bool:
        if not self.is_running():
            return False
        with self._lock:
            self._paused = True
            process = self._current_process
            suspended = process.suspend() if process and process.running else False
            self._set_status(paused=True, state="paused")
        if process and not suspended and process.running:
            logger.warning("Не удалось приостановить FFmpeg; пауза будет применена при следующем опросе")
        log_event("engine", "Эфир поставлен на паузу", "info")
        return True

    def resume(self) -> bool:
        if not self.is_running():
            return False
        with self._lock:
            self._paused = False
            process = self._current_process
            if process:
                process.resume()
            state = self._status.get("phase", "program")
            self._set_status(paused=False, state="playing" if state == "program" else state)
        log_event("engine", "Эфир продолжен", "info")
        return True

    def skip(self) -> bool:
        if not self.is_running():
            return False
        with self._lock:
            self._skip_requested = True
            self._paused = False
            process = self._current_process
        if process:
            process.terminate()
        return True

    def restart(self) -> bool:
        if not self.is_running():
            return False
        with self._lock:
            self._restart_requested = True
            self._paused = False
            process = self._current_process
        if process:
            process.terminate()
        return True

    def next_program(self) -> bool:
        if not self.is_running():
            return False
        with self._lock:
            self._next_program_requested = True
            self._paused = False
            process = self._current_process
        if process:
            process.terminate()
        return True

    def play_item(self, item_id: int) -> bool:
        if not self.is_running():
            return False
        with self._lock:
            self._override_item_id = int(item_id)
            self._paused = False
            process = self._current_process
        if process:
            process.terminate()
        return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            data = dict(self._status)
            process = self._current_process
        now = now_local()
        item_started = self._parse_dt(data.get("item_started_at"))
        program_started = self._parse_dt(data.get("program_started_at"))
        item_duration = data.get("item_duration")
        if item_started and item_duration:
            elapsed = max(0.0, (now - item_started).total_seconds())
            data["elapsed"] = round(elapsed, 1)
            data["remaining"] = max(0.0, round(float(item_duration) - elapsed, 1))
            data["progress"] = round(min(100.0, elapsed / float(item_duration) * 100), 1)
        if program_started:
            program_elapsed = max(0.0, (now - program_started).total_seconds())
            program_ends = self._parse_dt(data.get("program_ends_at"))
            if program_ends and program_ends > program_started:
                total = (program_ends - program_started).total_seconds()
                data["program_progress"] = round(
                    min(100.0, program_elapsed / total * 100), 1
                )
        data["paused"] = self._paused
        if self._paused:
            data["state"] = "paused"
        data["ffmpeg_running"] = bool(process and process.running)
        data["ffmpeg_pid"] = process.pid if process and process.running else None
        data["engine_running"] = self.is_running()
        data["viewers"] = stream_manager.viewers.count()
        data["hls_ready"] = stream_manager.ready()
        stats = stream_manager.stats()
        data["hls_segments"] = stats.get("segments", 0)
        data["hls_size"] = stats.get("size", 0)
        data["hls_playlist_age"] = stats.get("playlist_age")
        data["updated_at"] = now.isoformat()
        return data

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def _loop(self) -> None:
        with self.app.app_context():
            logger.info("Эфирный движок запущен")
            log_event("engine", "Эфирный движок запущен")
            self._set_status(state="starting")
            try:
                while not self._stop.is_set():
                    try:
                        mode = self._mode(force=True)
                        if mode != self._last_mode:
                            self._last_mode = mode
                            label = {"on": "включён", "off": "выключен", "maintenance": "профилактика"}.get(mode, mode)
                            log_event("channel", f"Режим канала: {label}")
                        self._set_status(mode=mode)
                        if mode == "off":
                            self._offline_loop()
                        elif mode == "maintenance":
                            self._maintenance_loop()
                        else:
                            self._live_loop()
                    except Exception as exc:  # pragma: no cover - defensive supervisor
                        logger.exception("Ошибка эфирного движка")
                        self._set_status(state="error", last_error=str(exc))
                        log_event("engine", f"Ошибка эфирного движка: {exc}", "error")
                        self._terminate_current()
                        self._sleep(3)
            finally:
                self._terminate_current()
                self._set_status(state="idle", phase="idle", paused=False)
                log_event("engine", "Эфирный движок остановлен")
                logger.info("Эфирный движок остановлен")

    def _offline_loop(self) -> None:
        self._terminate_current()
        self._set_status(
            state="offline",
            phase="offline",
            program_id=None,
            program_title="",
            item_id=None,
            media_id=None,
            media_title="",
            item_started_at=None,
            item_duration=None,
            paused=False,
            ffmpeg_running=False,
            progress=0,
        )
        while not self._stop.is_set():
            if self._mode() != "off":
                return
            self._sleep(1)

    # ------------------------------------------------------------------
    # Live programming
    # ------------------------------------------------------------------
    def _live_loop(self) -> None:
        program, item_index, program_started = self._resolve_start_program()
        if program is None:
            self._standby_loop()
            return
        self._log_program_start(program)
        while not self._stop.is_set():
            if self._mode() != "on":
                return
            if self._paused:
                self._wait_while_paused()
                if self._stop.is_set():
                    return
                if self._mode() != "on":
                    return
            self._handle_pending_controls_without_process(program, item_index)
            if self._next_program_requested:
                self._clear_controls()
                program, item_index, program_started = self._switch_next_program(program, manual=True)
                if program is None:
                    self._standby_loop()
                    return
                continue
            if self._override_item_id:
                override_id = self._override_item_id
                self._clear_controls()
                resolved = self._resolve_override(override_id)
                if resolved:
                    program, item_index, program_started = resolved
                continue

            db.session.expire_all()
            program = program_by_id(program.id if program else None)
            if program is None or not program.enabled:
                program, item_index, program_started = self._switch_next_program(None, manual=False)
                if program is None:
                    self._standby_loop()
                    return
                continue

            # Follow an active schedule entry when enabled.
            if self._respect_schedule() and not self._manual_active():
                active = scheduler.active_occurrence()
                if active and active.program_id != program.id:
                    candidate = program_by_id(active.program_id)
                    if candidate and candidate.enabled and candidate.items:
                        program = candidate
                        item_index = 0
                        program_started = now_local()
                        self._clear_controls()
                        self._log_program_start(program)
                        self._persist(program, None, "program", program_started)
                        continue

            items = list(program.items)
            if not items:
                self._log_program_end(program)
                program, item_index, program_started = self._switch_next_program(program, manual=False)
                if program is None:
                    self._standby_loop()
                    return
                continue
            if item_index >= len(items):
                self._log_program_end(program)
                self._set_status(state="program_end", phase="program_end", progress=100)
                self._persist(program, None, "program_end", program_started)
                ad_outcome = self._maybe_ad_after_program()
                if ad_outcome in {"stop", "mode"}:
                    return
                if ad_outcome == "next_program":
                    self._clear_controls()
                    program, item_index, program_started = self._switch_next_program(program, manual=True)
                    if program is None:
                        self._standby_loop()
                        return
                    continue
                if ad_outcome == "override":
                    override_id = self._override_item_id
                    self._clear_controls()
                    resolved = self._resolve_override(override_id)
                    if resolved:
                        program, item_index, program_started = resolved
                    continue
                program, item_index, program_started = self._switch_next_program(program, manual=False)
                if program is None:
                    self._standby_loop()
                    return
                continue

            item = items[item_index]
            if not item.media:
                log_event("broadcast", f"Элемент #{item.id} не содержит медиа", "warning")
                item_index += 1
                self._persist(program, item, "program", program_started)
                continue
            outcome = self._play_media_item(item, program, item_index, len(items), program_started)
            if outcome in {"stop", "mode"}:
                return
            if outcome == "skip":
                self._clear_controls()
                item_index += 1
                self._persist(program, item, "program", program_started)
                continue
            if outcome == "restart":
                self._clear_controls()
                continue
            if outcome == "next_program":
                self._clear_controls()
                program, item_index, program_started = self._switch_next_program(program, manual=True)
                if program is None:
                    self._standby_loop()
                    return
                continue
            if outcome == "override":
                override_id = self._override_item_id
                self._clear_controls()
                resolved = self._resolve_override(override_id)
                if resolved:
                    program, item_index, program_started = resolved
                continue

            # Normal end or failure: bumpers/ads/fallback.
            item_index += 1
            self._persist(program, item, "program", program_started)
            special_outcome = "ended"
            if outcome == "ended":
                special_outcome = self._maybe_bumper()
            else:
                log_event(
                    "broadcast",
                    f"Источник недоступен, пропуск: {item.media.title if item.media else item.id}",
                    "warning",
                )
                special_outcome = self._fallback_after_failure()
            if special_outcome in {"stop", "mode"}:
                return
            if special_outcome == "next_program":
                self._clear_controls()
                program, item_index, program_started = self._switch_next_program(program, manual=True)
                if program is None:
                    self._standby_loop()
                    return
                continue
            if special_outcome == "override":
                override_id = self._override_item_id
                self._clear_controls()
                resolved = self._resolve_override(override_id)
                if resolved:
                    program, item_index, program_started = resolved
                continue
            if outcome == "ended":
                ad_outcome = self._maybe_ad_after_video()
                if ad_outcome in {"stop", "mode"}:
                    return
                if ad_outcome == "next_program":
                    self._clear_controls()
                    program, item_index, program_started = self._switch_next_program(program, manual=True)
                    if program is None:
                        self._standby_loop()
                        return
                    continue
                if ad_outcome == "override":
                    override_id = self._override_item_id
                    self._clear_controls()
                    resolved = self._resolve_override(override_id)
                    if resolved:
                        program, item_index, program_started = resolved
                    continue

    def _resolve_start_program(self) -> tuple[Program | None, int, datetime]:
        state = get_playback_state()
        self._last_bumper_id = state.last_bumper_id
        self._last_ad_id = state.last_ad_id
        program = program_by_id(state.current_program_id)
        if program and program.enabled and program.items:
            items = list(program.items)
            item_index = 0
            if state.current_item_id:
                for index, item in enumerate(items):
                    if item.id == state.current_item_id:
                        item_index = index
                        break
            started = state.program_started_at or now_local()
            if now_local() - started > timedelta(hours=24):
                started = now_local()
            self._set_program_status(program, item_index, items, started)
            return program, item_index, started
        program = scheduler.choose_start_program()
        if program is None or not program.items:
            return None, 0, now_local()
        self._set_program_status(program, 0, list(program.items), now_local())
        return program, 0, now_local()

    def _play_media_item(
        self,
        item: ProgramItem,
        program: Program,
        item_index: int,
        items_total: int,
        program_started: datetime,
    ) -> str:
        media = item.media
        if media is None:
            return "failed"
        retries = max(0, get_int_setting("max_retries", Config.ENGINE_RETRIES))
        last_error = ""
        for attempt in range(retries + 1):
            if self._stop.is_set():
                return "stop"
            if self._mode() != "on":
                return "mode"
            try:
                spec = self._build_media_spec(media, force_ytdlp=attempt > 0)
                if attempt == 0:
                    log_event("broadcast", f"Начало видео: {media.title}")
                else:
                    log_event(
                        "broadcast",
                        f"Повторная попытка ({attempt}/{retries}): {media.title}",
                        "warning",
                    )
                self._set_status(
                    state="playing",
                    phase="program",
                    program_id=program.id,
                    program_title=program.title,
                    program_description=program.description or "",
                    item_id=item.id,
                    media_id=media.id,
                    media_title=media.title,
                    item_index=item_index,
                    items_total=items_total,
                    item_started_at=now_local().isoformat(),
                    item_duration=spec.duration,
                    program_started_at=program_started.isoformat(),
                    program_ends_at=self._program_end(program, program_started).isoformat(),
                    next_program_title=self._next_title(program),
                    last_error="",
                    paused=False,
                )
                self._persist(program, item, "program", program_started)
                process = ffmpeg_manager.start(spec, label=f"program:{media.id}")
                self._set_process(process)
                outcome = self._wait_process(process, spec.duration, expected_mode="on")
                self._clear_process(process)
                if outcome in {"stop", "mode", "skip", "restart", "next_program", "override"}:
                    return outcome
                if process.returncode == 0:
                    log_event("broadcast", f"Видео завершено: {media.title}")
                    self._mark_media_ok(media.id)
                    return "ended"
                last_error = process.tail(12) or f"FFmpeg завершился с кодом {process.returncode}"
                log_event(
                    "ffmpeg",
                    f"Ошибка воспроизведения «{media.title}»: {last_error[-1200:]}",
                    "error",
                )
            except (FFmpegError, YtDlpError, SecurityError, OSError, ValueError) as exc:
                last_error = str(exc)
                log_event(
                    "ffmpeg",
                    f"Не удалось запустить «{media.title}»: {last_error}",
                    "error",
                )
            except Exception as exc:  # pragma: no cover - defensive
                last_error = str(exc)
                logger.exception("Неожиданная ошибка воспроизведения")
                log_event("engine", f"Неожиданная ошибка воспроизведения: {last_error}", "error")
            finally:
                self._clear_process()
            if attempt < retries:
                self._sleep(2)
        self._mark_media_error(media.id, last_error)
        return "failed"

    def _build_media_spec(self, media: Media, force_ytdlp: bool = False) -> StreamSpec:
        settings = self._stream_settings()
        logo = self._logo_path()
        source_type = media.source_type
        if source_type == "local":
            if not media.file_path:
                raise FFmpegError("У локального видео отсутствует путь.")
            path = safe_join(Config.STORAGE_DIR, media.file_path)
            if not path.is_file():
                raise FFmpegError("Файл видео не найден на диске.")
            probe = ffmpeg_manager.probe(str(path), remote=False)
            self._update_media_probe(media.id, probe)
            return StreamSpec(
                source=str(path),
                source_kind="local",
                kind="program",
                probe=probe,
                duration=probe.duration,
                is_live=False,
                logo_path=logo,
                **settings,
            )

        if source_type in {"url", "hls"}:
            if not media.source_url:
                raise FFmpegError("У видео отсутствует URL.")
            url = validate_public_url(media.source_url)
            if source_type == "hls" or ".m3u8" in url.lower():
                validate_hls_playlist(url)
            probe: ProbeResult | None = None
            try:
                probe = ffmpeg_manager.probe(url, remote=True)
                self._update_media_probe(media.id, probe)
            except FFmpegError as exc:
                logger.warning("FFprobe не смог прочитать %s: %s", url, exc)
            duration = probe.duration if probe else media.duration
            is_live = bool(probe.is_live) if probe else not bool(duration)
            return StreamSpec(
                source=url,
                source_kind="url",
                kind="program",
                probe=probe,
                duration=duration,
                is_live=is_live,
                logo_path=logo,
                copy_allowed=probe is not None,
                **settings,
            )

        if source_type == "ytdlp":
            if not media.source_url:
                raise FFmpegError("У видео отсутствует ссылка на источник.")
            stream = ytdlp_manager.extract(media.source_url, force=force_ytdlp)
            probe: ProbeResult | None = None
            try:
                probe = ffmpeg_manager.probe(stream.url, headers=stream.headers, remote=True)
                if probe and not stream.audio_url and probe.has_audio:
                    self._update_media_probe(media.id, probe)
            except FFmpegError as exc:
                logger.warning("FFprobe не смог прочитать поток yt-dlp: %s", exc)
            duration = stream.duration or (probe.duration if probe else None) or media.duration
            is_live = bool(stream.is_live or (probe.is_live if probe else False))
            return StreamSpec(
                source=stream.url,
                source_kind="url",
                kind="program",
                audio_source=stream.audio_url,
                headers=stream.headers,
                audio_headers=stream.audio_headers,
                probe=probe,
                duration=duration,
                is_live=is_live,
                has_audio_override=bool(stream.audio_url or (probe.has_audio if probe else False)),
                logo_path=logo,
                copy_allowed=probe is not None and not stream.audio_url,
                **settings,
            )
        raise FFmpegError(f"Неизвестный тип источника: {source_type}")

    # ------------------------------------------------------------------
    # Special items
    # ------------------------------------------------------------------
    def _maybe_bumper(self) -> str:
        if not get_bool_setting("bumpers_enabled", True):
            return "ended"
        global_chance = get_int_setting("bumper_probability", 100)
        if global_chance <= 0 or self._rng.randint(1, 100) > global_chance:
            return "ended"
        bumpers = list(
            db.session.execute(select(Bumper).where(Bumper.enabled.is_(True))).scalars().all()
        )
        candidates = [item for item in bumpers if self._rng.randint(1, 100) <= max(1, item.probability)]
        if self._last_bumper_id and len(candidates) > 1:
            candidates = [item for item in candidates if item.id != self._last_bumper_id]
        if not candidates:
            return "ended"
        bumper = self._rng.choice(candidates)
        outcome = self._play_special_file(
            bumper.absolute_path, "bumper", bumper.title, bumper.id, "bumper"
        )
        if outcome == "ended":
            self._last_bumper_id = bumper.id
            self._persist_last_ids()
        return outcome

    def _fallback_after_failure(self) -> str:
        """Show a bumper if possible, otherwise a short standby card."""
        if get_bool_setting("bumpers_enabled", True):
            bumpers = list(
                db.session.execute(select(Bumper).where(Bumper.enabled.is_(True))).scalars().all()
            )
            if bumpers:
                candidates = [item for item in bumpers if item.id != self._last_bumper_id] or bumpers
                bumper = self._rng.choice(candidates)
                outcome = self._play_special_file(
                    bumper.absolute_path, "bumper", bumper.title, bumper.id, "bumper"
                )
                if outcome == "ended":
                    self._last_bumper_id = bumper.id
                    self._persist_last_ids()
                return outcome
        return self._play_standby_for(8)

    def _maybe_ad_after_video(self) -> str:
        if not get_bool_setting("ads_enabled", True):
            return "ended"
        chance = get_int_setting("ad_after_video_probability", 0)
        if chance <= 0 or self._rng.randint(1, 100) > chance:
            return "ended"
        return self._play_ad_block(1)

    def _maybe_ad_after_program(self) -> str:
        if not get_bool_setting("ads_enabled", True):
            return "ended"
        if not get_bool_setting("ad_after_program", True):
            return "ended"
        count = max(1, min(5, get_int_setting("ad_block_size", 1)))
        return self._play_ad_block(count)

    def _play_ad_block(self, count: int) -> str:
        ads = list(db.session.execute(select(Ad).where(Ad.enabled.is_(True))).scalars().all())
        if not ads:
            return "ended"
        pool = list(ads)
        for _ in range(max(1, count)):
            candidates = [item for item in pool if item.id != self._last_ad_id] or pool
            ad = self._rng.choice(candidates)
            outcome = self._play_special_file(ad.absolute_path, "ad", ad.title, ad.id, "ad")
            if outcome in {"stop", "mode", "skip", "restart", "next_program", "override"}:
                return outcome
            if outcome == "ended":
                self._last_ad_id = ad.id
                self._persist_last_ids()
                pool = [item for item in ads if item.id != ad.id] or ads
            else:
                pool = [item for item in pool if item.id != ad.id] or ads
        return "ended"

    def _play_special_file(
        self, path: Path, kind: str, title: str, item_id: int, state_name: str
    ) -> str:
        if not path.is_file():
            log_event("broadcast", f"Файл {kind} не найден: {path.name}", "error")
            return "failed"
        try:
            probe = ffmpeg_manager.probe(str(path), remote=False)
            settings = self._stream_settings()
            spec = StreamSpec(
                source=str(path),
                source_kind="local",
                kind=kind,
                probe=probe,
                duration=probe.duration,
                is_live=False,
                logo_path=None,
                **settings,
            )
            self._set_status(
                state=state_name,
                phase=state_name,
                media_title=title,
                item_duration=probe.duration,
                item_started_at=now_local().isoformat(),
                progress=0,
                elapsed=0,
                remaining=probe.duration,
                last_error="",
            )
            log_event("broadcast", f"Запуск {('рекламы' if kind == 'ad' else 'бампера')}: {title}")
            process = ffmpeg_manager.start(spec, label=f"{kind}:{item_id}")
            self._set_process(process)
            outcome = self._wait_process(process, probe.duration, expected_mode="on")
            self._clear_process(process)
            if outcome in {"stop", "mode", "skip", "restart", "next_program", "override"}:
                return outcome
            if process.returncode == 0:
                log_event("broadcast", f"Завершён {('рекламный ролик' if kind == 'ad' else 'бампер')}: {title}")
                return "ended"
            log_event("ffmpeg", f"Ошибка {kind} «{title}»: {process.tail(8)}", "error")
            return "failed"
        except (FFmpegError, OSError, ValueError) as exc:
            log_event("ffmpeg", f"Ошибка {kind} «{title}»: {exc}", "error")
            return "failed"
        finally:
            self._clear_process()

    # ------------------------------------------------------------------
    # Maintenance and standby
    # ------------------------------------------------------------------
    def _maintenance_loop(self) -> None:
        log_event("channel", "Канал переведён в режим профилактики")
        while not self._stop.is_set():
            if self._mode() != "maintenance":
                return
            if self._paused:
                self._wait_while_paused()
                if self._mode() != "maintenance":
                    return
            try:
                spec = self._maintenance_spec()
                self._set_status(
                    state="maintenance",
                    phase="maintenance",
                    program_title="Профилактика",
                    media_title="Профилактика",
                    item_started_at=now_local().isoformat(),
                    item_duration=None,
                    progress=0,
                    elapsed=0,
                    remaining=None,
                    paused=False,
                )
                process = ffmpeg_manager.start(spec, label="maintenance")
                self._set_process(process)
                outcome = self._wait_process(process, None, expected_mode="maintenance")
                self._clear_process(process)
                if outcome in {"stop", "mode"}:
                    return
                if outcome in {"skip", "restart", "next_program", "override"}:
                    self._clear_controls()
                    continue
                if process.returncode not in (0, None):
                    log_event("ffmpeg", f"Профилактика: FFmpeg завершился с кодом {process.returncode}", "warning")
            except (FFmpegError, OSError, ValueError) as exc:
                log_event("ffmpeg", f"Ошибка профилактики: {exc}", "error")
                self._clear_process()
            self._sleep(2)

    def _standby_loop(self) -> None:
        log_event("broadcast", "Нет доступных передач — режим ожидания")
        while not self._stop.is_set():
            if self._mode() != "on":
                return
            if enabled_programs():
                return
            try:
                spec = self._standby_spec()
                self._set_status(
                    state="standby",
                    phase="standby",
                    program_id=None,
                    program_title="Ожидание",
                    media_title="Заставка SIMPLE",
                    item_started_at=now_local().isoformat(),
                    item_duration=None,
                    progress=0,
                    elapsed=0,
                    remaining=None,
                    paused=False,
                )
                process = ffmpeg_manager.start(spec, label="standby")
                self._set_process(process)
                outcome = self._wait_process(process, None, expected_mode="on")
                self._clear_process(process)
                if outcome in {"stop", "mode"}:
                    return
                if outcome in {"skip", "restart", "next_program", "override"}:
                    self._clear_controls()
                    continue
            except (FFmpegError, OSError, ValueError) as exc:
                log_event("ffmpeg", f"Ошибка заставки ожидания: {exc}", "error")
                self._clear_process()
            self._sleep(2)

    def _play_standby_for(self, seconds: float) -> str:
        try:
            spec = self._standby_spec()
            self._set_status(state="standby", phase="standby", media_title="Резервная заставка")
            process = ffmpeg_manager.start(spec, label="fallback-standby")
            self._set_process(process)
            outcome = self._wait_process(process, None, expected_mode="on", max_seconds=seconds)
            self._clear_process(process)
            if outcome in {"stop", "mode"}:
                return outcome
            return "ended"
        except (FFmpegError, OSError, ValueError) as exc:
            log_event("ffmpeg", f"Не удалось показать резервную заставку: {exc}", "error")
            self._sleep(seconds)
            return "ended"
        finally:
            self._clear_process()

    def _maintenance_spec(self) -> StreamSpec:
        settings = self._stream_settings()
        video = get_setting("maintenance_video", "")
        image = get_setting("maintenance_image", "")
        music = get_setting("maintenance_music", "")
        music_enabled = get_bool_setting("maintenance_music_enabled", True)
        if video:
            path = safe_join(Config.STORAGE_DIR, video)
            if path.is_file():
                probe = ffmpeg_manager.probe(str(path), remote=False)
                return StreamSpec(
                    source=str(path),
                    source_kind="local",
                    kind="maintenance",
                    probe=probe,
                    duration=None,
                    is_live=False,
                    loop=True,
                    logo_path=None,
                    **settings,
                )
        if image:
            path = safe_join(Config.STORAGE_DIR, image)
            if path.is_file():
                audio_path = None
                if music_enabled and music:
                    candidate = safe_join(Config.STORAGE_DIR, music)
                    if candidate.is_file():
                        audio_path = str(candidate)
                return StreamSpec(
                    source=str(path),
                    source_kind="image",
                    kind="maintenance",
                    audio_source=audio_path,
                    loop=True,
                    loop_audio=True,
                    probe=None,
                    duration=None,
                    is_live=False,
                    logo_path=None,
                    **settings,
                )
        brand = self._brand_image("maintenance", get_setting("maintenance_message", "Технические работы"))
        if brand:
            return StreamSpec(
                source=str(brand),
                source_kind="image",
                kind="maintenance",
                loop=True,
                probe=None,
                duration=None,
                is_live=False,
                logo_path=None,
                **settings,
            )
        return StreamSpec(
            source="",
            source_kind="standby",
            kind="maintenance",
            probe=None,
            duration=None,
            is_live=False,
            logo_path=None,
            **settings,
        )

    def _standby_spec(self) -> StreamSpec:
        settings = self._stream_settings()
        brand = self._brand_image("standby", get_setting("standby_text", "SIMPLE — скоро в эфире"))
        if brand:
            return StreamSpec(
                source=str(brand),
                source_kind="image",
                kind="standby",
                loop=True,
                probe=None,
                duration=None,
                is_live=False,
                logo_path=None,
                **settings,
            )
        return StreamSpec(
            source="",
            source_kind="standby",
            kind="standby",
            probe=None,
            duration=None,
            is_live=False,
            logo_path=None,
            **settings,
        )

    def _brand_image(self, kind: str, text: str) -> Path | None:
        try:
            from PIL import Image, ImageDraw, ImageFont
        except Exception:
            return None
        settings = self._stream_settings()
        width = int(settings["output_width"])
        height = int(settings["output_height"])
        digest = hashlib.md5(f"{kind}|{text}|{width}x{height}".encode("utf-8")).hexdigest()[:10]
        target = Config.STORAGE_DIR / "intros" / f"{kind}_{digest}.png"
        if target.is_file():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (width, height), (8, 11, 16))
        draw = ImageDraw.Draw(image)
        # Subtle vertical gradient.
        for y in range(height):
            ratio = y / max(1, height - 1)
            color = (
                int(8 + 10 * ratio),
                int(11 + 14 * ratio),
                int(16 + 28 * ratio),
            )
            draw.line([(0, y), (width, y)], fill=color)
        font_brand = self._font(width, 0.09)
        font_text = self._font(width, 0.032)
        brand = "SIMPLE"
        bbox = draw.textbbox((0, 0), brand, font=font_brand)
        draw.text(
            ((width - (bbox[2] - bbox[0])) / 2, height * 0.38),
            brand,
            font=font_brand,
            fill=(244, 247, 252),
        )
        bbox = draw.textbbox((0, 0), text, font=font_text)
        draw.text(
            ((width - (bbox[2] - bbox[0])) / 2, height * 0.54),
            text,
            font=font_text,
            fill=(150, 163, 180),
        )
        draw.line(
            [(width * 0.42, height * 0.66), (width * 0.58, height * 0.66)],
            fill=(92, 111, 255),
            width=max(2, int(width * 0.002)),
        )
        try:
            image.save(target, format="PNG", optimize=True)
        except OSError:
            return None
        return target

    @staticmethod
    def _font(width: int, ratio: float):
        from PIL import ImageFont

        size = max(12, int(width * ratio))
        candidates = [
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        for candidate in candidates:
            try:
                if Path(candidate).is_file():
                    return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()

    # ------------------------------------------------------------------
    # Process supervision
    # ------------------------------------------------------------------
    def _wait_process(
        self,
        process: FFmpegProcess,
        duration: float | None,
        expected_mode: str,
        max_seconds: float | None = None,
    ) -> str:
        deadline = time.monotonic() + max_seconds if max_seconds else None
        while process.running:
            if self._stop.is_set():
                process.terminate()
                return "stop"
            if self._mode() != expected_mode:
                process.terminate()
                return "mode"
            if self._skip_requested:
                process.terminate()
                return "skip"
            if self._restart_requested:
                process.terminate()
                return "restart"
            if self._next_program_requested:
                process.terminate()
                return "next_program"
            if self._override_item_id:
                process.terminate()
                return "override"
            if self._paused:
                if not process._suspended:
                    process.suspend()
                self._set_status(state="paused", paused=True)
            else:
                if process._suspended:
                    process.resume()
                self._set_status(state=self._state_for_phase(), paused=False)
            if deadline and time.monotonic() >= deadline:
                process.terminate()
                return "ended"
            self._set_status(
                ffmpeg_running=True,
                ffmpeg_pid=process.pid,
                elapsed=round(process.progress_seconds, 1),
                progress=(
                    round(min(100.0, process.progress_seconds / duration * 100), 1)
                    if duration
                    else 0.0
                ),
                remaining=(
                    max(0.0, round(duration - process.progress_seconds, 1))
                    if duration
                    else None
                ),
            )
            time.sleep(Config.ENGINE_POLL_INTERVAL)
        # Process exited; controls still take priority over the natural end.
        if self._stop.is_set():
            return "stop"
        if self._mode() != expected_mode:
            return "mode"
        if self._skip_requested:
            return "skip"
        if self._restart_requested:
            return "restart"
        if self._next_program_requested:
            return "next_program"
        if self._override_item_id:
            return "override"
        return "ended"

    def _set_process(self, process: FFmpegProcess) -> None:
        with self._lock:
            self._current_process = process
            self._set_status(ffmpeg_running=True, ffmpeg_pid=process.pid)

    def _clear_process(self, process: FFmpegProcess | None = None) -> None:
        with self._lock:
            if process is None or self._current_process is process:
                self._current_process = None
            self._set_status(ffmpeg_running=False, ffmpeg_pid=None)

    def _terminate_current(self) -> None:
        with self._lock:
            process = self._current_process
        if process:
            process.terminate()

    def _state_for_phase(self) -> str:
        phase = self._status.get("phase", "program")
        if phase in {"bumper", "ad"}:
            return phase
        if phase == "maintenance":
            return "maintenance"
        if phase == "standby":
            return "standby"
        return "playing"

    def _wait_while_paused(self) -> None:
        while self._paused and not self._stop.is_set():
            if self._mode() not in {"on", "maintenance"}:
                return
            self._sleep(0.3)

    # ------------------------------------------------------------------
    # Queue helpers
    # ------------------------------------------------------------------
    def _handle_pending_controls_without_process(self, program: Program, item_index: int) -> None:
        # Controls are normally consumed by _wait_process; this handles a
        # control pressed in the tiny gap between two items.
        if self._restart_requested:
            self._clear_controls()
        elif self._skip_requested:
            self._clear_controls()

    def _switch_next_program(
        self, current: Program | None, manual: bool
    ) -> tuple[Program | None, int, datetime]:
        next_program = self._pick_next_program(current, manual=manual)
        if next_program is None:
            return None, 0, now_local()
        self._clear_controls()
        started = now_local()
        if manual:
            self._manual_until = started + timedelta(minutes=10)
        self._log_program_start(next_program)
        self._persist(next_program, None, "program", started)
        self._set_program_status(next_program, 0, list(next_program.items), started)
        return next_program, 0, started

    def _pick_next_program(self, current: Program | None, manual: bool = False) -> Program | None:
        respect = self._respect_schedule() and not manual and not self._manual_active()
        candidate: Program | None = None
        if respect:
            candidate = scheduler.choose_next_program(
                current.id if current else None, now_local(), respect_schedule=True
            )
        if candidate is None or not candidate.items:
            programs = enabled_programs()
            if not programs:
                return None
            if current is None:
                candidates = programs
            else:
                ids = [item.id for item in programs]
                start = ids.index(current.id) + 1 if current.id in ids else 0
                candidates = programs[start:] + programs[:start]
            for item in candidates:
                if item.items:
                    return item
            return None
        return candidate

    def _resolve_override(self, item_id: int | None) -> tuple[Program, int, datetime] | None:
        if not item_id:
            return None
        item = db.session.get(ProgramItem, int(item_id))
        if item is None or item.program is None:
            log_event("engine", "Выбранный для эфира элемент не найден", "warning")
            return None
        program = item.program
        if not program.enabled or not program.items:
            return None
        items = list(program.items)
        index = next((position for position, row in enumerate(items) if row.id == item.id), 0)
        started = now_local()
        self._manual_until = started + timedelta(minutes=10)
        self._log_program_start(program)
        self._persist(program, item, "program", started)
        self._set_program_status(program, index, items, started)
        return program, index, started

    def _respect_schedule(self) -> bool:
        return get_bool_setting("respect_schedule", True)

    def _manual_active(self) -> bool:
        return bool(self._manual_until and now_local() < self._manual_until)

    def _clear_controls(self) -> None:
        with self._lock:
            self._skip_requested = False
            self._restart_requested = False
            self._next_program_requested = False
            self._override_item_id = None

    # ------------------------------------------------------------------
    # Settings / metadata / persistence
    # ------------------------------------------------------------------
    def _mode(self, force: bool = False) -> str:
        now = time.monotonic()
        if force or now - self._mode_checked > 0.7:
            self._mode_value = get_setting("channel_mode", "on") or "on"
            self._mode_checked = now
        return self._mode_value

    def _stream_settings(self) -> dict[str, Any]:
        return {
            "output_width": get_int_setting("output_width", Config.OUTPUT_WIDTH),
            "output_height": get_int_setting("output_height", Config.OUTPUT_HEIGHT),
            "output_fps": get_int_setting("output_fps", Config.OUTPUT_FPS),
            "video_bitrate": get_setting("video_bitrate", Config.VIDEO_BITRATE) or Config.VIDEO_BITRATE,
            "audio_bitrate": get_setting("audio_bitrate", Config.AUDIO_BITRATE) or Config.AUDIO_BITRATE,
            "hls_time": get_int_setting("hls_time", Config.HLS_TIME),
            "hls_list_size": get_int_setting("hls_list_size", Config.HLS_LIST_SIZE),
            "hls_dir": Config.HLS_DIR / "stream",
            "start_number": 0,
        }

    def _logo_path(self) -> str | None:
        if not get_bool_setting("logo_enabled", True):
            return None
        relative = get_setting("logo_file", "")
        if not relative:
            return None
        try:
            path = safe_join(Config.STORAGE_DIR, relative)
        except SecurityError:
            return None
        return str(path) if path.is_file() else None

    def _program_end(self, program: Program, started: datetime) -> datetime:
        active = scheduler.active_occurrence()
        if active and active.program_id == program.id:
            return active.end
        duration = scheduler.program_duration_seconds(program)
        return started + timedelta(seconds=max(60, duration))

    def _next_title(self, program: Program | None) -> str:
        following = scheduler.next_occurrence()
        if following:
            return following.title
        if program:
            candidate = scheduler.rotation_next(program.id)
            if candidate and candidate.id != program.id:
                return candidate.title
        return ""

    def _set_program_status(
        self, program: Program, item_index: int, items: list[ProgramItem], started: datetime
    ) -> None:
        self._set_status(
            program_id=program.id,
            program_title=program.title,
            program_description=program.description or "",
            item_index=item_index,
            items_total=len(items),
            program_started_at=started.isoformat(),
            program_ends_at=self._program_end(program, started).isoformat(),
            next_program_title=self._next_title(program),
        )

    def _log_program_start(self, program: Program) -> None:
        log_event("broadcast", f"Начало передачи: {program.title}")

    def _log_program_end(self, program: Program) -> None:
        log_event("broadcast", f"Завершение передачи: {program.title}")

    def _persist(
        self,
        program: Program | None,
        item: ProgramItem | None,
        phase: str,
        program_started: datetime | None,
    ) -> None:
        try:
            save_playback_state(
                current_program_id=program.id if program else None,
                current_item_id=item.id if item else None,
                current_media_id=item.media_id if item else None,
                phase=phase,
                item_started_at=now_local() if item else None,
                program_started_at=program_started,
                last_bumper_id=self._last_bumper_id,
                last_ad_id=self._last_ad_id,
            )
        except Exception:
            db.session.rollback()
            logger.exception("Не удалось сохранить состояние эфира")

    def _persist_last_ids(self) -> None:
        try:
            save_playback_state(last_bumper_id=self._last_bumper_id, last_ad_id=self._last_ad_id)
        except Exception:
            db.session.rollback()

    def _update_media_probe(self, media_id: int, probe: ProbeResult) -> None:
        try:
            media = db.session.get(Media, media_id)
            if media is None:
                return
            media.width = probe.width or media.width
            media.height = probe.height or media.height
            media.fps = probe.fps or media.fps
            media.vcodec = probe.vcodec or media.vcodec
            media.acodec = probe.acodec or media.acodec
            media.has_audio = probe.has_audio
            media.aspect = probe.aspect or media.aspect
            media.pix_fmt = probe.pix_fmt or media.pix_fmt
            media.sample_rate = probe.sample_rate or media.sample_rate
            media.duration = probe.duration or media.duration
            media.last_checked = now_local()
            media.status = "ok"
            media.last_error = None
            db.session.commit()
        except Exception:
            db.session.rollback()

    def _mark_media_ok(self, media_id: int) -> None:
        try:
            media = db.session.get(Media, media_id)
            if media:
                media.status = "ok"
                media.last_error = None
                media.last_checked = now_local()
                db.session.commit()
        except Exception:
            db.session.rollback()

    def _mark_media_error(self, media_id: int, error: str) -> None:
        try:
            media = db.session.get(Media, media_id)
            if media:
                media.status = "error"
                media.last_error = clean_text(error, 1000)
                media.last_checked = now_local()
                db.session.commit()
        except Exception:
            db.session.rollback()

    def _set_status(self, **values: Any) -> None:
        with self._lock:
            self._status.update(values)
            self._status["updated_at"] = now_local().isoformat()

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while not self._stop.is_set() and time.monotonic() < deadline:
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    @staticmethod
    def _parse_dt(value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
