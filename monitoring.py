"""System monitoring helpers (psutil)."""
from __future__ import annotations

import os
import threading
import time
from typing import Any

import psutil

from config import Config
from ffmpeg_manager import ffmpeg_manager
from utils import human_size, uptime_string

_lock = threading.Lock()
_previous_net: tuple[float, int, int] | None = None
_started_at = time.time()


def init_monitoring() -> None:
    global _started_at
    _started_at = time.time()
    # Prime psutil's non-blocking CPU counter.
    psutil.cpu_percent(interval=None)
    _network_snapshot()


def app_uptime() -> float:
    return max(0.0, time.time() - _started_at)


def _network_snapshot() -> tuple[float, int, int]:
    counters = psutil.net_io_counters()
    return time.time(), int(counters.bytes_sent), int(counters.bytes_recv)


def network_rates() -> dict[str, float]:
    global _previous_net
    now = time.time()
    sent, recv = psutil.net_io_counters().bytes_sent, psutil.net_io_counters().bytes_recv
    with _lock:
        if _previous_net is None:
            _previous_net = (now, sent, recv)
            return {"sent_per_sec": 0.0, "recv_per_sec": 0.0}
        previous_time, previous_sent, previous_recv = _previous_net
        elapsed = max(0.001, now - previous_time)
        _previous_net = (now, sent, recv)
    return {
        "sent_per_sec": max(0.0, (sent - previous_sent) / elapsed),
        "recv_per_sec": max(0.0, (recv - previous_recv) / elapsed),
    }


def count_ffmpeg_processes() -> int:
    count = 0
    for process in psutil.process_iter(["name"]):
        try:
            name = (process.info.get("name") or "").lower()
            if name in {"ffmpeg", "ffmpeg.exe"}:
                count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return count


def system_metrics() -> dict[str, Any]:
    cpu = psutil.cpu_percent(interval=None)
    memory = psutil.virtual_memory()
    try:
        disk = psutil.disk_usage(str(Config.BASE_DIR))
    except OSError:
        disk = psutil.disk_usage("/")
    net = network_rates()
    hls = ffmpeg_manager.hls_stats()
    load = None
    try:
        load = os.getloadavg()
    except (AttributeError, OSError):
        pass
    return {
        "cpu": round(cpu, 1),
        "cpu_count": psutil.cpu_count(logical=True),
        "memory_percent": round(memory.percent, 1),
        "memory_used": memory.used,
        "memory_total": memory.total,
        "memory_available": memory.available,
        "memory_used_label": human_size(memory.used),
        "memory_total_label": human_size(memory.total),
        "memory_available_label": human_size(memory.available),
        "disk_percent": round(disk.percent, 1),
        "disk_used": disk.used,
        "disk_total": disk.total,
        "disk_free": disk.free,
        "disk_used_label": human_size(disk.used),
        "disk_total_label": human_size(disk.total),
        "disk_free_label": human_size(disk.free),
        "net_sent_per_sec": net["sent_per_sec"],
        "net_recv_per_sec": net["recv_per_sec"],
        "net_sent_label": human_size(net["sent_per_sec"]) + "/с",
        "net_recv_label": human_size(net["recv_per_sec"]) + "/с",
        "uptime": app_uptime(),
        "uptime_label": uptime_string(app_uptime()),
        "load": [round(value, 2) for value in load] if load else None,
        "ffmpeg_processes": count_ffmpeg_processes(),
        "hls_segments": hls["segments"],
        "hls_size": hls["size"],
        "hls_size_label": human_size(hls["size"]),
        "hls_playlist": hls["playlist"],
    }
