"""Security helpers: SSRF protection, safe paths and small validators."""
from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path
from urllib.parse import urljoin, urlparse

from config import Config

VIEWER_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
TAG_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9 _\-,]{1,40}$")


class SecurityError(ValueError):
    """Raised when user input is unsafe."""


def _forbidden_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def validate_public_url(url: str, resolve: bool = True) -> str:
    """Allow only public HTTP(S) URLs. Blocks localhost/private/metadata hosts."""
    if not url or not isinstance(url, str):
        raise SecurityError("URL не указан.")
    url = url.strip()
    if len(url) > Config.MAX_URL_LENGTH:
        raise SecurityError("URL слишком длинный.")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise SecurityError("Разрешены только ссылки http:// и https://.")
    if parsed.username or parsed.password:
        raise SecurityError("URL с логином или паролем не разрешён.")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise SecurityError("В URL отсутствует домен.")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(
        (".local", ".internal", ".localhost", ".home.arpa")
    ):
        raise SecurityError("Локальные адреса запрещены.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in Config.ALLOWED_URL_PORTS:
        raise SecurityError("Разрешены только стандартные порты 80 и 443.")
    # Literal IP addresses are checked directly.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _forbidden_ip(str(literal)):
            raise SecurityError("Приватные и служебные IP-адреса запрещены.")
        return url
    if resolve:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise SecurityError("Не удалось разрешить домен источника.") from exc
        if not infos:
            raise SecurityError("Домен источника не найден.")
        for info in infos:
            address = info[4][0]
            if _forbidden_ip(address):
                raise SecurityError("Источник указывает на запрещённый IP-адрес.")
    return url


def validate_hls_playlist(url: str, timeout: float = 10.0, depth: int = 2) -> None:
    """Best-effort validation of an HLS manifest's referenced hosts.

    FFmpeg follows playlists itself, so we pre-fetch a user supplied manifest
    and verify that it does not point at local services.
    """
    import urllib.error
    import urllib.request

    validate_public_url(url)
    if depth <= 0:
        return
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (SIMPLE IPTV local validation)",
            "Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,*/*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            validate_public_url(final_url)
            data = response.read(512 * 1024).decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise SecurityError(f"Не удалось проверить HLS-плейлист: {exc}") from exc
    if "#EXTM3U" not in data[:200]:
        raise SecurityError("Ссылка не является HLS-плейлистом.")
    uris: list[str] = []
    is_master = "#EXT-X-STREAM-INF" in data
    for line in data.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        uris.append(urljoin(final_url, line))
    if not uris:
        raise SecurityError("HLS-плейлист не содержит сегментов.")
    # Validate every distinct host, and recurse into master playlists.
    checked: set[str] = set()
    for item in uris[:80]:
        parsed = urlparse(item)
        host_key = f"{parsed.scheme}://{parsed.netloc}"
        if host_key in checked:
            continue
        checked.add(host_key)
        validate_public_url(item)
    if is_master:
        for item in uris[:3]:
            try:
                validate_hls_playlist(item, timeout=timeout, depth=depth - 1)
            except SecurityError:
                # Variant validation is best effort; the host was already checked.
                continue


def safe_join(root: Path | str, *parts: str | Path) -> Path:
    """Join a path under root and reject traversal/symlink escapes."""
    base = Path(root).resolve()
    candidate = base.joinpath(*[str(part) for part in parts]).resolve()
    if candidate != base and base not in candidate.parents:
        raise SecurityError("Недопустимый путь к файлу.")
    return candidate


def relative_under(root: Path | str, path: Path | str) -> str:
    base = Path(root).resolve()
    candidate = Path(path).resolve()
    if candidate != base and base not in candidate.parents:
        raise SecurityError("Файл находится вне каталога хранения.")
    return candidate.relative_to(base).as_posix()


def validate_viewer_id(value: str | None) -> str:
    if not value or not VIEWER_RE.fullmatch(value):
        raise SecurityError("Некорректный идентификатор зрителя.")
    return value


def sanitize_tags(value: str | None) -> str:
    if not value:
        return ""
    tags = []
    for raw in value.replace(";", ",").split(","):
        tag = raw.strip()
        if not tag:
            continue
        if not TAG_RE.fullmatch(tag):
            continue
        if tag.lower() not in {existing.lower() for existing in tags}:
            tags.append(tag)
    return ", ".join(tags[:12])


def validate_repeat_days(value: str | None) -> str:
    if not value:
        return ""
    days = []
    for part in str(value).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            day = int(part)
        except ValueError:
            continue
        if 0 <= day <= 6 and str(day) not in days:
            days.append(str(day))
    return ",".join(days)


def is_safe_stream_segment(name: str) -> bool:
    return bool(re.fullmatch(r"(index\.m3u8|seg_\d{6,12}\.ts)", name))
