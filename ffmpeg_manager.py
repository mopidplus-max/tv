"""FFmpeg/FFprobe engine: adaptive probing, HLS commands and process control."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from config import Config
from utils import clamp, get_logger, now_local, parse_float, run_command

logger = get_logger("ffmpeg")


class FFmpegError(RuntimeError):
    pass


@dataclass
class ProbeResult:
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec: str = ""
    acodec: str = ""
    has_video: bool = False
    has_audio: bool = False
    duration: float | None = None
    aspect: str = ""
    pix_fmt: str = ""
    sample_rate: int = 0
    format_name: str = ""
    bit_rate: int = 0
    size: int = 0
    is_live: bool = False

    @property
    def resolution_label(self) -> str:
        return f"{self.width}×{self.height}" if self.width and self.height else "—"

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "vcodec": self.vcodec,
            "acodec": self.acodec,
            "has_video": self.has_video,
            "has_audio": self.has_audio,
            "duration": self.duration,
            "aspect": self.aspect,
            "pix_fmt": self.pix_fmt,
            "sample_rate": self.sample_rate,
            "format_name": self.format_name,
            "bit_rate": self.bit_rate,
            "size": self.size,
            "is_live": self.is_live,
        }


@dataclass
class StreamSpec:
    source: str
    source_kind: str = "local"  # local | url | image | standby
    kind: str = "program"  # program | bumper | ad | maintenance | standby
    audio_source: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    audio_headers: dict[str, str] = field(default_factory=dict)
    duration: float | None = None
    is_live: bool = False
    loop: bool = False
    loop_audio: bool = False
    logo_path: str | None = None
    probe: ProbeResult | None = None
    has_audio_override: bool | None = None
    copy_allowed: bool = True
    output_width: int = Config.OUTPUT_WIDTH
    output_height: int = Config.OUTPUT_HEIGHT
    output_fps: int = Config.OUTPUT_FPS
    video_bitrate: str = Config.VIDEO_BITRATE
    audio_bitrate: str = Config.AUDIO_BITRATE
    hls_time: int = Config.HLS_TIME
    hls_list_size: int = Config.HLS_LIST_SIZE
    hls_dir: Path = Config.HLS_DIR / "stream"
    start_number: int = 0
    standby_text: str = ""

    @property
    def has_audio(self) -> bool:
        if self.has_audio_override is not None:
            return self.has_audio_override
        return bool(self.probe and self.probe.has_audio)


class FFmpegProcess:
    """A single supervised FFmpeg process with progress and stderr capture."""

    def __init__(self, command: list[str], label: str = "ffmpeg") -> None:
        self.command = command
        self.label = label
        self.process: subprocess.Popen[str] | None = None
        self.progress_seconds = 0.0
        self.progress_speed = ""
        self.progress_fps = ""
        self.progress_bitrate = ""
        self.stderr_tail: deque[str] = deque(maxlen=160)
        self._threads: list[threading.Thread] = []
        self._suspended = False
        self.started_at: float | None = None

    def start(self) -> "FFmpegProcess":
        creationflags = 0
        if sys.platform.startswith("win"):
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
            creationflags=creationflags,
            start_new_session=not sys.platform.startswith("win"),
        )
        self.started_at = time.time()
        for target in (self._read_stdout, self._read_stderr):
            thread = threading.Thread(target=target, name=f"{self.label}-reader", daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        try:
            for line in self.process.stdout:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key == "out_time_us":
                    try:
                        self.progress_seconds = max(0.0, int(value) / 1_000_000)
                    except ValueError:
                        pass
                elif key == "out_time_ms":
                    # Some builds report microseconds under this legacy name.
                    try:
                        candidate = int(value) / 1_000_000
                        if candidate >= self.progress_seconds:
                            self.progress_seconds = candidate
                    except ValueError:
                        pass
                elif key == "out_time" and not self.progress_seconds:
                    match = re.match(r"(\d+):(\d+):(\d+\.?\d*)", value)
                    if match:
                        hours, minutes, seconds = match.groups()
                        self.progress_seconds = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
                elif key == "speed":
                    self.progress_speed = value
                elif key == "fps":
                    self.progress_fps = value
                elif key == "bitrate":
                    self.progress_bitrate = value
        except (ValueError, OSError):
            pass

    def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        try:
            for line in self.process.stderr:
                line = line.rstrip()
                if not line:
                    continue
                self.stderr_tail.append(line)
                if any(token in line.lower() for token in ("error", "failed", "invalid", "unable")):
                    logger.warning("%s: %s", self.label, line)
                else:
                    logger.debug("%s: %s", self.label, line)
        except (ValueError, OSError):
            pass

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process else None

    @property
    def returncode(self) -> int | None:
        return self.process.poll() if self.process else None

    @property
    def running(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def wait(self, timeout: float | None = None) -> int | None:
        if not self.process:
            return None
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def terminate(self, timeout: float = 4.0) -> None:
        if not self.process:
            return
        if self._suspended:
            self.resume()
        if self.process.poll() is not None:
            return
        try:
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        except OSError:
            pass

    def suspend(self) -> bool:
        if not self.process or self.process.poll() is not None:
            return False
        if self._suspended:
            return True
        try:
            proc = psutil.Process(self.process.pid)
            proc.suspend()
            for child in proc.children(recursive=True):
                try:
                    child.suspend()
                except psutil.Error:
                    pass
            self._suspended = True
            return True
        except (psutil.Error, AttributeError, OSError):
            return False

    def resume(self) -> bool:
        if not self.process or not self._suspended:
            return False
        try:
            proc = psutil.Process(self.process.pid)
            for child in proc.children(recursive=True):
                try:
                    child.resume()
                except psutil.Error:
                    pass
            proc.resume()
            self._suspended = False
            return True
        except (psutil.Error, OSError):
            return False

    def tail(self, lines: int = 20) -> str:
        return "\n".join(list(self.stderr_tail)[-lines:])


class FFmpegManager:
    def __init__(self) -> None:
        self.ffmpeg = Config.FFMPEG_BIN
        self.ffprobe = Config.FFPROBE_BIN
        self._version_cache: dict[str, str] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Discovery / diagnostics
    # ------------------------------------------------------------------
    def version(self, binary: str | None = None) -> str:
        target = binary or self.ffmpeg
        if target in self._version_cache:
            return self._version_cache[target]
        try:
            result = run_command([target, "-version"], timeout=10)
            first = (result.stdout or result.stderr or "").splitlines()[0]
            self._version_cache[target] = first.strip()
            return first.strip()
        except Exception as exc:
            return f"недоступен: {exc}"

    def ffprobe_version(self) -> str:
        return self.version(self.ffprobe)

    def binaries(self) -> dict[str, Any]:
        ffmpeg_path = shutil.which(self.ffmpeg) or (self.ffmpeg if Path(self.ffmpeg).exists() else "")
        ffprobe_path = shutil.which(self.ffprobe) or (self.ffprobe if Path(self.ffprobe).exists() else "")
        return {
            "ffmpeg": {"path": ffmpeg_path or self.ffmpeg, "version": self.version(self.ffmpeg), "ok": bool(ffmpeg_path)},
            "ffprobe": {
                "path": ffprobe_path or self.ffprobe,
                "version": self.version(self.ffprobe),
                "ok": bool(ffprobe_path),
            },
        }

    def check_binaries(self) -> dict[str, Any]:
        result = self.binaries()
        ytdlp_ok = False
        ytdlp_version = "не установлен"
        try:
            import yt_dlp  # noqa: F401
            import importlib.metadata

            ytdlp_version = importlib.metadata.version("yt-dlp")
            ytdlp_ok = True
        except Exception:
            pass
        result["ytdlp"] = {"path": "python:yt-dlp", "version": ytdlp_version, "ok": ytdlp_ok}
        return result

    # ------------------------------------------------------------------
    # Probing
    # ------------------------------------------------------------------
    def probe(
        self,
        source: str,
        headers: dict[str, str] | None = None,
        remote: bool | None = None,
        timeout: float | None = None,
        require_video: bool = True,
    ) -> ProbeResult:
        if not source:
            raise FFmpegError("Источник не указан.")
        if remote is None:
            remote = source.startswith(("http://", "https://"))
        command = [
            self.ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "-analyzeduration",
            "6000000",
            "-probesize",
            "6000000",
        ]
        if remote:
            command += [
                "-rw_timeout",
                str(max(5, int(Config.URL_TIMEOUT)) * 1_000_000),
                "-protocol_whitelist",
                "http,https,tcp,tls,crypto",
                "-allowed_extensions",
                "ALL",
            ]
            ua, extra = self._header_args(headers or {})
            if ua:
                command += ["-user_agent", ua]
            if extra:
                command += ["-headers", extra]
        else:
            command += ["-protocol_whitelist", "file"]
        command.append(source)
        try:
            result = run_command(command, timeout=timeout or Config.FFMPEG_PROBE_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise FFmpegError("FFprobe не ответил за отведённое время.") from exc
        if result.returncode != 0:
            message = (result.stderr or "").strip().splitlines()
            raise FFmpegError(message[-1] if message else "FFprobe не смог прочитать источник.")
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise FFmpegError("FFprobe вернул некорректный JSON.") from exc
        streams = data.get("streams") or []
        video = next((item for item in streams if item.get("codec_type") == "video"), None)
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        if video is None and require_video:
            raise FFmpegError("В источнике отсутствует видеодорожка.")
        fmt = data.get("format") or {}
        if video is not None:
            fps = self._parse_rate(video.get("avg_frame_rate")) or self._parse_rate(
                video.get("r_frame_rate")
            )
        else:
            fps = 0.0
        duration = self._first_float(
            fmt.get("duration"),
            video.get("duration") if video else None,
            audio.get("duration") if audio else None,
        )
        if duration is not None and duration <= 0:
            duration = None
        width = int(video.get("width") or 0) if video else 0
        height = int(video.get("height") or 0) if video else 0
        aspect = (video.get("display_aspect_ratio") or "") if video else ""
        if not aspect and width and height:
            divisor = math.gcd(width, height)
            aspect = f"{width // divisor}:{height // divisor}"
        return ProbeResult(
            width=width,
            height=height,
            fps=fps or 0.0,
            vcodec=str(video.get("codec_name") or "") if video else "",
            acodec=str(audio.get("codec_name") or "") if audio else "",
            has_video=video is not None,
            has_audio=audio is not None,
            duration=duration,
            aspect=str(aspect),
            pix_fmt=str(video.get("pix_fmt") or "") if video else "",
            sample_rate=int(audio.get("sample_rate") or 0) if audio else 0,
            format_name=str(fmt.get("format_name") or ""),
            bit_rate=int(parse_float(fmt.get("bit_rate"), 0)),
            size=int(parse_float(fmt.get("size"), 0)),
            is_live=bool(remote and duration is None),
        )

    @staticmethod
    def _first_float(*values: Any) -> float | None:
        for value in values:
            try:
                if value is None:
                    continue
                number = float(value)
                if number > 0:
                    return number
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _parse_rate(value: Any) -> float:
        if not value:
            return 0.0
        text = str(value)
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            try:
                denominator_value = float(denominator)
                return float(numerator) / denominator_value if denominator_value else 0.0
            except (TypeError, ValueError):
                return 0.0
        try:
            return float(text)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _header_args(headers: dict[str, str]) -> tuple[str, str]:
        if not headers:
            return "", ""
        user_agent = ""
        lines = []
        for key, value in headers.items():
            if not key or value is None:
                continue
            if key.lower() == "user-agent":
                user_agent = str(value)
            else:
                lines.append(f"{key}: {value}")
        return user_agent, "\r\n".join(lines) + ("\r\n" if lines else "")

    # ------------------------------------------------------------------
    # Command building
    # ------------------------------------------------------------------
    def _output_geometry(self, spec: StreamSpec) -> tuple[int, int, int]:
        width = max(160, int(spec.output_width))
        height = max(90, int(spec.output_height))
        width -= width % 2
        height -= height % 2
        fps = int(clamp(spec.output_fps or 30, 15, 60))
        return width, height, fps

    def _bitrate_for_probe(self, spec: StreamSpec) -> str:
        probe = spec.probe
        if not probe or not probe.height:
            return spec.video_bitrate
        height = probe.height
        if height <= 480:
            return "1400k"
        if height <= 720:
            return "2200k"
        if height <= 1080:
            return "3000k"
        return "3500k"

    def _logo_geometry(self, out_w: int, out_h: int) -> tuple[int, int, int, int]:
        # Reference layout: 342x74 logo, 100 px right and 120 px top on 1920x1080.
        max_logo_w = max(40, out_w - 16)
        logo_w = int(round(out_w * 342 / 1920))
        logo_h = int(round(logo_w * 74 / 342))
        right = int(round(out_w * 100 / 1920))
        top = int(round(out_h * 120 / 1080))
        right = max(8, right)
        top = max(8, top)
        logo_w = max(24, min(logo_w, max_logo_w - right))
        logo_h = max(8, min(logo_h, out_h - top - 8))
        logo_w -= logo_w % 2
        logo_h -= logo_h % 2
        return max(2, logo_w), max(2, logo_h), right, top

    def build_command(self, spec: StreamSpec) -> list[str]:
        out_w, out_h, fps = self._output_geometry(spec)
        inputs: list[str] = []
        input_count = 0

        def add_input(args: list[str]) -> int:
            nonlocal input_count
            index = input_count
            inputs.extend(args)
            input_count += 1
            return index

        source_index = 0
        if spec.source_kind == "standby":
            source_index = add_input(
                [
                    "-re",
                    "-f",
                    "lavfi",
                    "-i",
                    f"color=c=0x090c12:s={out_w}x{out_h}:r={fps}",
                ]
            )
        elif spec.source_kind == "image":
            source_index = add_input(
                ["-re", "-loop", "1", "-framerate", str(fps), "-i", spec.source]
            )
        elif spec.source_kind == "url":
            args: list[str] = []
            if not spec.is_live:
                args.append("-re")
            args += [
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_delay_max",
                "5",
                "-rw_timeout",
                str(max(5, int(Config.URL_TIMEOUT)) * 1_000_000),
                "-protocol_whitelist",
                "http,https,tcp,tls,crypto",
                "-allowed_extensions",
                "ALL",
            ]
            ua, extra = self._header_args(spec.headers)
            if ua:
                args += ["-user_agent", ua]
            if extra:
                args += ["-headers", extra]
            args += ["-i", spec.source]
            source_index = add_input(args)
        else:
            args = []
            if spec.loop:
                args += ["-stream_loop", "-1"]
            if not spec.is_live:
                args += ["-re"]
            args += ["-protocol_whitelist", "file", "-i", spec.source]
            source_index = add_input(args)

        audio_index: int | None = None
        if spec.audio_source:
            args = []
            if spec.loop_audio:
                args += ["-stream_loop", "-1"]
            if not spec.is_live:
                args += ["-re"]
            if spec.audio_source.startswith(("http://", "https://")):
                args += [
                    "-reconnect",
                    "1",
                    "-reconnect_streamed",
                    "1",
                    "-reconnect_delay_max",
                    "5",
                    "-protocol_whitelist",
                    "http,https,tcp,tls,crypto",
                ]
                ua, extra = self._header_args(spec.audio_headers)
                if ua:
                    args += ["-user_agent", ua]
                if extra:
                    args += ["-headers", extra]
            else:
                args += ["-protocol_whitelist", "file"]
            args += ["-i", spec.audio_source]
            audio_index = add_input(args)

        logo_index: int | None = None
        if spec.logo_path and Path(spec.logo_path).exists():
            logo_index = add_input(["-loop", "1", "-i", spec.logo_path])

        anull_index: int | None = None
        has_source_audio = spec.has_audio and audio_index is None
        if not has_source_audio and audio_index is None:
            anull_index = add_input(
                ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            )

        # Copy mode: only clean H.264 sources without overlays and with a
        # browser-compatible audio track (or silent audio that we encode).
        copy_video = bool(
            spec.copy_allowed
            and logo_index is None
            and spec.probe
            and spec.probe.vcodec == "h264"
            and spec.kind not in {"standby"}
        )
        if copy_video and not has_source_audio and audio_index is None:
            copy_audio = False
        else:
            copy_audio = bool(
                copy_video
                and has_source_audio
                and spec.probe
                and spec.probe.acodec in {"aac", "mp3"}
            )

        command = [self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning", "-y"]
        command += inputs

        video_filter = (
            f"[{source_index}:v]scale=w={out_w}:h={out_h}:"
            "force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps={fps},format=yuv420p"
        )
        if logo_index is not None:
            logo_w, logo_h, right, top = self._logo_geometry(out_w, out_h)
            video_filter += "[base]"
            filter_complex = (
                f"{video_filter};"
                f"[{logo_index}:v]scale={logo_w}:{logo_h}:flags=lanczos,format=rgba[logo];"
                f"[base][logo]overlay=W-w-{right}:{top}:format=auto,format=yuv420p[vout]"
            )
        else:
            video_filter += "[vout]"
            filter_complex = video_filter

        audio_label = ""
        if not copy_audio:
            if has_source_audio:
                audio_label = f"{source_index}:a"
            elif audio_index is not None:
                audio_label = f"{audio_index}:a"
            elif anull_index is not None:
                audio_label = f"{anull_index}:a"
            if audio_label:
                filter_complex += (
                    f";[{audio_label}]aresample=48000,"
                    "aformat=sample_fmts=fltp:channel_layouts=stereo[aout]"
                )

        if copy_video:
            command += ["-map", f"{source_index}:v:0"]
        else:
            command += ["-filter_complex", filter_complex, "-map", "[vout]"]

        if copy_audio:
            command += ["-map", f"{source_index}:a:0", "-c:a", "copy"]
        elif audio_label:
            command += ["-map", "[aout]", "-c:a", "aac", "-b:a", spec.audio_bitrate, "-ar", "48000", "-ac", "2"]
        else:
            command += ["-an"]

        if copy_video:
            command += ["-c:v", "copy"]
        else:
            bitrate = self._bitrate_for_probe(spec)
            gop = max(1, int(round(fps * max(1, spec.hls_time))))
            command += [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-profile:v",
                "main",
                "-level",
                "4.0",
                "-pix_fmt",
                "yuv420p",
                "-b:v",
                bitrate,
                "-maxrate",
                bitrate,
                "-bufsize",
                f"{int(parse_float(bitrate.rstrip('k'), 2200) * 2)}k",
                "-g",
                str(gop),
                "-keyint_min",
                str(gop),
                "-sc_threshold",
                "0",
                "-force_key_frames",
                f"expr:gte(t,n_forced*{max(1, spec.hls_time)})",
            ]

        if spec.duration and spec.duration > 0 and not spec.loop and not spec.is_live:
            # Input ends naturally; -shortest protects finite audio + video pairs.
            command += ["-shortest"]
        elif anull_index is not None and spec.duration and spec.duration > 0 and not spec.loop:
            command += ["-shortest"]

        hls_flags = "delete_segments+append_list+omit_endlist+program_date_time"
        if not copy_video:
            hls_flags += "+independent_segments"
        hls_dir = Path(spec.hls_dir)
        hls_dir.mkdir(parents=True, exist_ok=True)
        command += [
            "-max_muxing_queue_size",
            "2048",
            "-f",
            "hls",
            "-hls_time",
            str(max(1, spec.hls_time)),
            "-hls_list_size",
            str(max(3, spec.hls_list_size)),
            "-hls_delete_threshold",
            "4",
            "-hls_flags",
            hls_flags,
            "-hls_segment_type",
            "mpegts",
            "-hls_segment_filename",
            str(hls_dir / "seg_%06d.ts"),
            "-start_number",
            str(max(0, spec.start_number)),
            "-progress",
            "pipe:1",
            "-nostats",
            str(hls_dir / "index.m3u8"),
        ]
        return command

    def start(self, spec: StreamSpec, label: str = "stream") -> FFmpegProcess:
        command = self.build_command(spec)
        logger.debug("FFmpeg: %s", " ".join(command))
        process = FFmpegProcess(command, label=label)
        try:
            process.start()
        except OSError as exc:
            raise FFmpegError(f"Не удалось запустить FFmpeg: {exc}") from exc
        return process

    # ------------------------------------------------------------------
    # HLS helpers
    # ------------------------------------------------------------------
    def hls_dir(self) -> Path:
        path = Config.HLS_DIR / "stream"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def playlist_path(self) -> Path:
        return self.hls_dir() / "index.m3u8"

    def playlist_text(self) -> str:
        path = self.playlist_path()
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def hls_stats(self) -> dict[str, Any]:
        directory = self.hls_dir()
        segments = list(directory.glob("seg_*.ts"))
        total = sum(item.stat().st_size for item in segments if item.exists())
        playlist = self.playlist_path()
        return {
            "segments": len(segments),
            "size": total,
            "playlist": playlist.exists(),
            "playlist_mtime": playlist.stat().st_mtime if playlist.exists() else None,
            "directory": str(directory),
        }

    def cleanup_hls(self, max_age: int | None = None) -> int:
        directory = self.hls_dir()
        max_age = max_age or Config.HLS_DIR_MAX_AGE
        playlist = self.playlist_text()
        referenced = {
            match.group(1)
            for match in re.finditer(r"^(seg_\d+\.ts)$", playlist, flags=re.MULTILINE)
        }
        now = time.time()
        removed = 0
        for item in directory.glob("seg_*.ts"):
            try:
                if item.name in referenced:
                    continue
                if now - item.stat().st_mtime > max_age:
                    item.unlink()
                    removed += 1
            except OSError:
                continue
        for item in directory.glob("*.tmp"):
            try:
                if now - item.stat().st_mtime > max_age:
                    item.unlink()
            except OSError:
                pass
        return removed

    def reset_hls(self) -> None:
        directory = self.hls_dir()
        for item in directory.iterdir():
            try:
                if item.is_file():
                    item.unlink()
            except OSError:
                pass

    def next_start_number(self) -> int:
        playlist = self.playlist_text()
        numbers = [int(match) for match in re.findall(r"seg_(\d+)\.ts", playlist)]
        return max(numbers) + 1 if numbers else 0

    def hls_ready(self) -> bool:
        text = self.playlist_text()
        return "#EXTM3U" in text and "seg_" in text

    def make_test_hls(self, directory: Path, duration: int = 3) -> dict[str, Any]:
        """Generate a tiny isolated HLS stream for diagnostics (never the live one)."""
        directory.mkdir(parents=True, exist_ok=True)
        for item in directory.glob("*"):
            try:
                item.unlink()
            except OSError:
                pass
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=640x360:rate=25:duration={max(2, duration)}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={max(2, duration)}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-f",
            "hls",
            "-hls_time",
            "1",
            "-hls_list_size",
            "5",
            "-hls_flags",
            "delete_segments+omit_endlist",
            "-hls_segment_filename",
            str(directory / "seg_%06d.ts"),
            str(directory / "index.m3u8"),
        ]
        result = run_command(command, timeout=max(30, duration * 6))
        playlist = directory / "index.m3u8"
        ok = result.returncode == 0 and playlist.exists() and "seg_" in playlist.read_text(
            encoding="utf-8", errors="replace"
        )
        return {
            "ok": ok,
            "returncode": result.returncode,
            "stderr": (result.stderr or "").strip()[-2000:],
            "playlist": str(playlist),
        }


ffmpeg_manager = FFmpegManager()
