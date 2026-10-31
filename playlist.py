"""M3U/M3U8 playlist generation."""
from __future__ import annotations

from flask import Request, request

from config import Config
from database import get_setting


def public_base_url(req: Request | None = None) -> str:
    configured = (get_setting("public_base_url", "") or Config.PUBLIC_BASE_URL or "").strip()
    if configured:
        return configured.rstrip("/")
    req = req or request
    return req.url_root.rstrip("/")


def stream_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/live/index.m3u8"


def epg_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/epg.xml.gz"


def logo_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/logo.png"


def build_m3u(base_url: str) -> str:
    channel_id = (get_setting("channel_id", "simple") or "simple").strip() or "simple"
    channel_name = (get_setting("channel_name", "SIMPLE") or "SIMPLE").strip() or "SIMPLE"
    description = (get_setting("channel_description", "") or "").strip()
    lines = [
        f'#EXTM3U x-tvg-url="{epg_url(base_url)}" url-tvg="{epg_url(base_url)}"',
        f'#EXTINF:-1 tvg-id="{channel_id}" tvg-name="{channel_name}" '
        f'tvg-logo="{logo_url(base_url)}" group-title="{channel_name}",{channel_name}',
    ]
    if description:
        lines.append(f"#EXTGRP:{channel_name}")
    lines.append(stream_url(base_url))
    return "\n".join(lines) + "\n"


def playlist_filename() -> str:
    name = (get_setting("channel_name", "SIMPLE") or "SIMPLE").strip().lower()
    safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in name)
    return f"{safe or 'simple'}.m3u"
