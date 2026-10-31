"""Small shared helpers used across SIMPLE."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterable

_LOGGERS: dict[str, logging.Logger] = {}


def _console_stream(stream):
    """Keep the console encoding but never crash on unencodable characters.

    Windows consoles often use cp1251/cp866, which cannot encode characters
    such as the multiplication sign used in resolution labels.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
    return stream


def setup_logging(log_dir: Path, level: str = "INFO") -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("simple")
    if getattr(root, "_simple_configured", False):
        return root
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        log_dir / "simple.log", maxBytes=4 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    console = logging.StreamHandler(_console_stream(sys.stdout))
    console.setFormatter(formatter)
    root.addHandler(console)
    root._simple_configured = True  # type: ignore[attr-defined]
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"simple.{name}")


def now_local() -> datetime:
    """Naive local time, used for schedules and XMLTV."""
    return datetime.now().replace(microsecond=0)


def local_tz():
    return datetime.now().astimezone().tzinfo or timezone.utc


def fmt_dt(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    return value.strftime(fmt) if value else "—"


def fmt_time(value: datetime | None) -> str:
    return value.strftime("%H:%M") if value else "—"


def fmt_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "—"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def human_size(size: int | float | None) -> str:
    if size is None:
        return "—"
    value = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{value:.1f} {unit}" if unit != "Б" else f"{int(value)} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ"


def parse_int(value: Any, default: int = 0, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def parse_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "да"}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def clean_text(value: str | None, max_length: int = 500, default: str = "") -> str:
    if not value:
        return default
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value)).strip()
    return text[:max_length]


def safe_json_loads(value: str | None, default: Any = None) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return default


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def run_command(
    args: Iterable[str],
    timeout: float | None = None,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command without a shell. Arguments are always a list."""
    command = [str(item) for item in args]
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
        env=env,
        shell=False,
        creationflags=creationflags,
        check=False,
    )


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding=encoding)
    os.replace(temporary, path)


def remove_file_quietly(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def uptime_string(seconds: float) -> str:
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} д")
    parts.append(f"{hours:02d}:{minutes:02d}:{secs:02d}")
    return " ".join(parts)


def relative_time(value: datetime | None) -> str:
    if not value:
        return "—"
    delta = (now_local() - value).total_seconds()
    if delta < 60:
        return "только что"
    if delta < 3600:
        return f"{int(delta // 60)} мин назад"
    if delta < 86400:
        return f"{int(delta // 3600)} ч назад"
    return fmt_dt(value)


def day_name(index: int) -> str:
    return ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][index % 7]


def next_seconds_boundary(seconds: int = 1) -> None:
    """Sleep until the next wall-clock boundary (used by lightweight loops)."""
    now = time.time()
    time.sleep(max(0.0, seconds - (now % seconds)))


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index : index + size] for index in range(0, len(items), size)]
