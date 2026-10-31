"""yt-dlp integration that only extracts direct stream URLs (never downloads)."""
from __future__ import annotations

import importlib.metadata
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

from config import Config
from security import SecurityError, validate_public_url
from utils import get_logger, now_local

logger = get_logger("ytdlp")


class YtDlpError(RuntimeError):
    pass


@dataclass
class ExtractedStream:
    url: str
    title: str = ""
    audio_url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    audio_headers: dict[str, str] = field(default_factory=dict)
    duration: float | None = None
    is_live: bool = False
    thumbnail: str = ""
    uploader: str = ""
    extractor: str = ""
    webpage_url: str = ""
    expires_at: datetime | None = None

    @property
    def expired(self) -> bool:
        return bool(self.expires_at and self.expires_at <= now_local())

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "audio_url": self.audio_url,
            "title": self.title,
            "duration": self.duration,
            "is_live": self.is_live,
            "thumbnail": self.thumbnail,
            "uploader": self.uploader,
            "extractor": self.extractor,
            "webpage_url": self.webpage_url,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


class YtDlpManager:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, ExtractedStream]] = {}
        self._available: bool | None = None

    def available(self) -> bool:
        if not Config.YTDLP_ENABLED:
            return False
        if self._available is None:
            try:
                import yt_dlp  # noqa: F401

                self._available = True
            except Exception:
                self._available = False
        return bool(self._available)

    def version(self) -> str:
        try:
            return importlib.metadata.version("yt-dlp")
        except importlib.metadata.PackageNotFoundError:
            return "не установлен"

    def _cache_get(self, url: str, force: bool) -> ExtractedStream | None:
        if force:
            return None
        cached = self._cache.get(url)
        if not cached:
            return None
        created, stream = cached
        ttl = max(60, int(Config.YTDLP_CACHE_TTL))
        if time.time() - created > ttl or stream.expired:
            self._cache.pop(url, None)
            return None
        return stream

    def _cache_put(self, url: str, stream: ExtractedStream) -> None:
        self._cache[url] = (time.time(), stream)
        if len(self._cache) > 200:
            oldest = sorted(self._cache.items(), key=lambda item: item[1][0])[:50]
            for key, _ in oldest:
                self._cache.pop(key, None)

    def clear_cache(self, url: str | None = None) -> None:
        if url:
            self._cache.pop(url, None)
        else:
            self._cache.clear()

    def extract(self, webpage_url: str, force: bool = False) -> ExtractedStream:
        if not self.available():
            raise YtDlpError("yt-dlp не установлен. Установите пакет yt-dlp.")
        try:
            validate_public_url(webpage_url)
        except SecurityError as exc:
            raise YtDlpError(str(exc)) from exc

        cached = self._cache_get(webpage_url, force)
        if cached:
            return cached

        try:
            import yt_dlp
        except Exception as exc:  # pragma: no cover - dependency issue
            raise YtDlpError("yt-dlp недоступен.") from exc

        base_options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "cachedir": False,
            "socket_timeout": max(5, int(Config.YTDLP_SOCKET_TIMEOUT)),
            "retries": 2,
            "fragment_retries": 2,
            "extractor_retries": 2,
            "nocheckcertificate": False,
            "ignoreerrors": False,
            "no_color": True,
            "consoletitle": False,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                )
            },
        }
        formats = [
            "bestvideo*[height<=1080]+bestaudio/best[height<=1080]/best",
            "best[height<=1080]/best",
        ]
        last_error: Exception | None = None
        info: dict[str, Any] | None = None
        for format_selector in formats:
            options = dict(base_options)
            options["format"] = format_selector
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    result = ydl.extract_info(webpage_url, download=False)
                if result:
                    if result.get("_type") == "playlist":
                        entries = result.get("entries") or []
                        if not entries:
                            raise YtDlpError("Плейлист не содержит доступных видео.")
                        result = entries[0]
                    info = result
                    break
            except Exception as exc:
                last_error = exc
                logger.warning("yt-dlp формат %s не сработал: %s", format_selector, exc)
        if info is None:
            raise YtDlpError(f"yt-dlp не смог извлечь поток: {last_error or 'неизвестная ошибка'}")

        stream = self._build_stream(info, webpage_url)
        self._cache_put(webpage_url, stream)
        logger.info(
            "yt-dlp извлёк поток: %s (%s), live=%s",
            stream.title or webpage_url,
            stream.extractor or "?",
            stream.is_live,
        )
        return stream

    def _build_stream(self, info: dict[str, Any], webpage_url: str) -> ExtractedStream:
        headers = {
            str(key): str(value)
            for key, value in (info.get("http_headers") or {}).items()
            if value is not None
        }
        requested = info.get("requested_formats") or []
        video_url = ""
        audio_url = ""
        video_headers = dict(headers)
        audio_headers = dict(headers)
        if requested:
            videos = [fmt for fmt in requested if fmt.get("vcodec") not in (None, "none")]
            audios = [fmt for fmt in requested if fmt.get("acodec") not in (None, "none")]
            if videos:
                video_url = videos[0].get("url") or ""
                video_headers.update(
                    {str(k): str(v) for k, v in (videos[0].get("http_headers") or {}).items()}
                )
            if audios:
                audio_url = audios[0].get("url") or ""
                audio_headers.update(
                    {str(k): str(v) for k, v in (audios[0].get("http_headers") or {}).items()}
                )
        if not video_url:
            video_url = info.get("url") or ""
        if not video_url:
            # Last chance: pick the best direct format.
            formats = info.get("formats") or []
            candidates = [fmt for fmt in formats if fmt.get("url")]
            if candidates:
                best = candidates[-1]
                video_url = best.get("url") or ""
                video_headers.update(
                    {str(k): str(v) for k, v in (best.get("http_headers") or {}).items()}
                )
                if best.get("acodec") in (None, "none"):
                    audio_url = ""
        if not video_url:
            raise YtDlpError("Источник не вернул прямой URL медиапотока.")
        try:
            validate_public_url(video_url)
            if audio_url:
                validate_public_url(audio_url)
        except SecurityError as exc:
            raise YtDlpError(f"Небезопасный URL потока: {exc}") from exc

        duration = info.get("duration")
        try:
            duration = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None
        expires_at = self._url_expiry(video_url)
        if expires_at is None:
            expires_at = now_local() + timedelta(seconds=max(300, int(Config.YTDLP_CACHE_TTL)))
        return ExtractedStream(
            url=video_url,
            audio_url=audio_url or None,
            title=str(info.get("title") or "").strip(),
            headers=video_headers,
            audio_headers=audio_headers,
            duration=duration,
            is_live=bool(info.get("is_live")),
            thumbnail=str(info.get("thumbnail") or ""),
            uploader=str(info.get("uploader") or info.get("channel") or ""),
            extractor=str(info.get("extractor_key") or info.get("extractor") or ""),
            webpage_url=str(info.get("webpage_url") or webpage_url),
            expires_at=expires_at,
        )

    @staticmethod
    def _url_expiry(url: str) -> datetime | None:
        try:
            query = parse_qs(urlparse(url).query)
        except Exception:
            return None
        for key in ("expire", "expires", "expiry"):
            values = query.get(key)
            if values:
                try:
                    return datetime.fromtimestamp(int(values[0]))
                except (TypeError, ValueError, OSError):
                    continue
        return None

    def metadata(self, webpage_url: str, force: bool = False) -> ExtractedStream:
        return self.extract(webpage_url, force=force)


ytdlp_manager = YtDlpManager()
