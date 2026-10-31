"""Validated uploads for videos, images, bumpers, ads and maintenance assets."""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from config import Config
from ffmpeg_manager import ProbeResult, ffmpeg_manager
from security import SecurityError, relative_under, safe_join
from utils import clean_text, get_logger

logger = get_logger("uploads")

VIDEO_MIME = {
    "video/mp4",
    "video/x-matroska",
    "video/webm",
    "video/quicktime",
    "video/x-msvideo",
    "video/mp2t",
    "application/octet-stream",
}
AUDIO_MIME = {
    "audio/mpeg",
    "audio/mp3",
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "audio/ogg",
    "audio/opus",
    "audio/flac",
    "audio/x-flac",
    "application/ogg",
}
IMAGE_MIME = {"image/png", "image/jpeg", "image/webp"}


class UploadError(ValueError):
    pass


def _file_size(file: FileStorage) -> int:
    stream = file.stream
    position = stream.tell()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    if position:
        try:
            stream.seek(position)
        except OSError:
            stream.seek(0)
    return int(size)


def _validate_extension(filename: str, allowed: set[str]) -> str:
    suffix = Path(filename or "").suffix.lower()
    if not suffix or suffix not in allowed:
        raise UploadError(
            f"Недопустимый формат файла. Разрешены: {', '.join(sorted(allowed))}."
        )
    return suffix


def _validate_mime(file: FileStorage, allowed: set[str]) -> None:
    mime = (file.mimetype or "").lower()
    if mime and mime not in allowed:
        raise UploadError(f"Недопустимый MIME-тип файла: {mime}.")


def _save_temp(file: FileStorage) -> Path:
    tmp_dir = Config.STORAGE_DIR / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "").suffix.lower()
    temp = tmp_dir / f"upload_{uuid.uuid4().hex}{suffix}"
    file.save(temp)
    return temp


def _final_path(kind: str, suffix: str) -> Path:
    directory = safe_join(Config.STORAGE_DIR, kind)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{uuid.uuid4().hex}{suffix}"


def _move(temp: Path, final: Path) -> None:
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temp, final)


def _cleanup_temp(temp: Path) -> None:
    try:
        if temp.exists():
            temp.unlink()
    except OSError:
        pass


def save_video(file: FileStorage, kind: str = "videos") -> tuple[Path, str, ProbeResult, int]:
    if not file or not file.filename:
        raise UploadError("Файл не выбран.")
    suffix = _validate_extension(file.filename, Config.VIDEO_EXTENSIONS)
    _validate_mime(file, VIDEO_MIME)
    size = _file_size(file)
    if size <= 0:
        raise UploadError("Файл пустой.")
    if size > Config.MAX_VIDEO_UPLOAD:
        raise UploadError("Видеофайл слишком большой.")
    temp = _save_temp(file)
    try:
        probe = ffmpeg_manager.probe(str(temp), remote=False)
        if not probe.has_video:
            raise UploadError("Файл не содержит видеодорожку.")
        final = _final_path(kind, suffix)
        _move(temp, final)
        return final, relative_under(Config.STORAGE_DIR, final), probe, size
    except Exception:
        _cleanup_temp(temp)
        raise


def save_audio(file: FileStorage, kind: str = "maintenance") -> tuple[Path, str, ProbeResult, int]:
    if not file or not file.filename:
        raise UploadError("Аудиофайл не выбран.")
    suffix = _validate_extension(file.filename, Config.AUDIO_EXTENSIONS)
    _validate_mime(file, AUDIO_MIME)
    size = _file_size(file)
    if size <= 0:
        raise UploadError("Файл пустой.")
    if size > Config.MAX_AUDIO_UPLOAD:
        raise UploadError("Аудиофайл слишком большой.")
    temp = _save_temp(file)
    try:
        probe = ffmpeg_manager.probe(str(temp), remote=False, require_video=False)
        if not probe.has_audio:
            raise UploadError("Файл не содержит аудиодорожку.")
        final = _final_path(kind, suffix)
        _move(temp, final)
        return final, relative_under(Config.STORAGE_DIR, final), probe, size
    except Exception:
        _cleanup_temp(temp)
        raise


def save_image(
    file: FileStorage,
    kind: str = "maintenance",
    exact_size: tuple[int, int] | None = None,
) -> tuple[Path, str, int, int]:
    from PIL import Image, UnidentifiedImageError

    if not file or not file.filename:
        raise UploadError("Изображение не выбрано.")
    suffix = _validate_extension(file.filename, {".png", ".jpg", ".jpeg", ".webp"})
    _validate_mime(file, IMAGE_MIME)
    size = _file_size(file)
    if size <= 0:
        raise UploadError("Файл пустой.")
    if size > Config.MAX_IMAGE_UPLOAD:
        raise UploadError("Изображение слишком большое.")
    temp = _save_temp(file)
    try:
        with Image.open(temp) as image:
            image.verify()
        with Image.open(temp) as image:
            width, height = image.size
            if exact_size and (width, height) != exact_size:
                raise UploadError(
                    f"Неверное разрешение: {width}×{height}. Требуется "
                    f"{exact_size[0]}×{exact_size[1]} px."
                )
            image_format = (image.format or "").upper()
            if exact_size and image_format != "PNG":
                raise UploadError("Логотип должен быть в формате PNG.")
            if image_format == "PNG":
                output_suffix = ".png"
                image = image.convert("RGBA" if exact_size else image.mode)
            elif image_format in {"JPEG", "WEBP"}:
                output_suffix = ".jpg" if image_format == "JPEG" else ".webp"
            else:
                raise UploadError("Неподдерживаемый формат изображения.")
            final = _final_path(kind, output_suffix)
            save_format = "PNG" if output_suffix == ".png" else image_format
            if save_format == "JPEG" and image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            image.save(final, format=save_format, optimize=True)
            _cleanup_temp(temp)
            return final, relative_under(Config.STORAGE_DIR, final), width, height
    except UploadError:
        _cleanup_temp(temp)
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        _cleanup_temp(temp)
        raise UploadError("Файл не является корректным изображением.") from exc


def save_logo(file: FileStorage) -> tuple[Path, str, int, int]:
    return save_image(file, "logo", exact_size=Config.LOGO_SIZE)


def save_cover(file: FileStorage) -> tuple[Path, str, int, int]:
    return save_image(file, "covers")


def save_ad(file: FileStorage) -> tuple[Path, str, ProbeResult, int]:
    final, relative, probe, size = save_video(file, "ads")
    if probe.width and probe.height:
        ratio = probe.width / probe.height
        if not 1.70 <= ratio <= 1.86:
            delete_stored(relative)
            raise UploadError(
                f"Рекламный ролик должен быть 16:9. Получено {probe.width}×{probe.height}."
            )
    return final, relative, probe, size


def save_bumper(file: FileStorage) -> tuple[Path, str, ProbeResult, int]:
    return save_video(file, "bumpers")


def delete_stored(relative_path: str | None) -> bool:
    if not relative_path:
        return False
    try:
        target = safe_join(Config.STORAGE_DIR, relative_path)
    except SecurityError:
        return False
    try:
        if target.is_file():
            target.unlink()
            return True
    except OSError:
        logger.warning("Не удалось удалить файл %s", target)
    return False


def suggested_title(filename: str, fallback: str = "Видео") -> str:
    stem = Path(filename or "").stem
    title = clean_text(stem.replace("_", " ").replace("-", " "), 200, fallback)
    return title or fallback
