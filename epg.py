"""XMLTV EPG generation (XML and GZIP)."""
from __future__ import annotations

import gzip
from datetime import datetime
from xml.etree import ElementTree as ET

from database import get_setting
from scheduler import schedule_epg_occurrences
from utils import local_tz, now_local


def _xmltv_time(value: datetime) -> str:
    tz = local_tz()
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value.astimezone(tz).strftime("%Y%m%d%H%M%S %z")


def _channel_id() -> str:
    return (get_setting("channel_id", "simple") or "simple").strip() or "simple"


def build_epg_xml(base_url: str, days: int = 7) -> str:
    base = (base_url or "").rstrip("/")
    channel_id = _channel_id()
    channel_name = get_setting("channel_name", "SIMPLE") or "SIMPLE"
    description = get_setting("channel_description", "") or ""
    root = ET.Element("tv", {"generator-info-name": "SIMPLE IPTV"})
    channel = ET.SubElement(root, "channel", {"id": channel_id})
    ET.SubElement(channel, "display-name", {"lang": "ru"}).text = channel_name
    if description:
        ET.SubElement(channel, "desc", {"lang": "ru"}).text = description
    logo_file = get_setting("logo_file", "")
    if logo_file:
        ET.SubElement(channel, "icon", {"src": f"{base}/logo.png"})

    seen: set[tuple[str, str, str]] = set()
    for occurrence in schedule_epg_occurrences(days=days, now=now_local()):
        key = (occurrence.title, occurrence.start.isoformat(), occurrence.end.isoformat())
        if key in seen:
            continue
        seen.add(key)
        programme = ET.SubElement(
            root,
            "programme",
            {
                "start": _xmltv_time(occurrence.start),
                "stop": _xmltv_time(occurrence.end),
                "channel": channel_id,
            },
        )
        ET.SubElement(programme, "title", {"lang": "ru"}).text = occurrence.title
        if occurrence.description:
            ET.SubElement(programme, "desc", {"lang": "ru"}).text = occurrence.description
        ET.SubElement(programme, "category", {"lang": "ru"}).text = "Передача"
        ET.SubElement(programme, "episode-num", {"system": "onscreen"}).text = str(
            occurrence.program_id
        )
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def build_epg_gzip(base_url: str, days: int = 7) -> bytes:
    xml_bytes = build_epg_xml(base_url, days=days).encode("utf-8")
    return gzip.compress(xml_bytes, compresslevel=9, mtime=0)
