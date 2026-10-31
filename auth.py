"""Authentication, CSRF and login throttling for the hidden admin area."""
from __future__ import annotations

import hmac
import secrets
import time
from datetime import timedelta
from functools import wraps
from typing import Callable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from flask import abort, current_app, jsonify, redirect, request, session, url_for
from sqlalchemy import select

from database import LoginAttempt, User, db, log_event
from utils import clean_text, get_logger, now_local

logger = get_logger("auth")
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)
_DUMMY_HASH = _hasher.hash("simple-dummy-password-for-timing")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except Exception:
        return False


def validate_password_strength(password: str) -> str | None:
    if not password or len(password) < 10:
        return "Пароль должен содержать не менее 10 символов."
    if len(password) > 256:
        return "Пароль слишком длинный."
    classes = sum(
        bool(any(check(char) for char in password))
        for check in (
            str.islower,
            str.isupper,
            str.isdigit,
            lambda char: not char.isalnum(),
        )
    )
    if classes < 3:
        return "Пароль должен содержать строчные и заглавные буквы, цифры или символы."
    return None


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _submitted_csrf() -> str:
    return (
        request.headers.get("X-CSRF-Token")
        or request.form.get("csrf_token")
        or (request.get_json(silent=True) or {}).get("csrf_token", "")
    )


def csrf_valid() -> bool:
    expected = session.get("csrf_token")
    supplied = _submitted_csrf()
    return bool(expected and supplied and hmac.compare_digest(str(expected), str(supplied)))


def require_csrf() -> None:
    if not csrf_valid():
        if request.path.startswith(current_app.config["ADMIN_PATH"]) and (
            request.is_json or request.path.endswith("/api/status")
        ):
            abort(400, description="CSRF token missing or invalid")
        abort(400, description="CSRF token missing or invalid")


def is_authenticated() -> bool:
    return bool(session.get("admin_user_id"))


def session_expired() -> bool:
    if not is_authenticated():
        return False
    timeout = int(current_app.config.get("ADMIN_SESSION_MINUTES", 60)) * 60
    last = float(session.get("last_activity", 0) or 0)
    return (time.time() - last) > timeout


def touch_session() -> None:
    session["last_activity"] = time.time()
    session.permanent = True


def login_user(user: User) -> None:
    session.clear()
    session["admin_user_id"] = user.id
    session["admin_username"] = user.username
    session["csrf_token"] = secrets.token_urlsafe(32)
    session["last_activity"] = time.time()
    session.permanent = True


def logout_user() -> None:
    session.clear()


def current_user() -> User | None:
    user_id = session.get("admin_user_id")
    if not user_id:
        return None
    return db.session.get(User, int(user_id))


def _attempt_key(ip: str, username: str) -> str:
    return f"{ip}|{username.strip().lower()[:120]}"


def check_login_allowed(ip: str, username: str) -> tuple[bool, str]:
    key = _attempt_key(ip, username)
    row = db.session.execute(
        select(LoginAttempt).where(LoginAttempt.key == key)
    ).scalar_one_or_none()
    now = now_local()
    if row is None:
        return True, ""
    if row.locked_until and row.locked_until > now:
        remaining = max(1, int((row.locked_until - now).total_seconds() // 60) + 1)
        return False, f"Слишком много попыток. Повторите через {remaining} мин."
    window = timedelta(minutes=int(current_app.config["LOGIN_WINDOW_MINUTES"]))
    if row.last_attempt and now - row.last_attempt > window:
        db.session.delete(row)
        db.session.commit()
        return True, ""
    if row.attempts >= int(current_app.config["LOGIN_MAX_ATTEMPTS"]):
        row.locked_until = now + timedelta(minutes=int(current_app.config["LOGIN_LOCK_MINUTES"]))
        row.last_attempt = now
        db.session.commit()
        return False, "Слишком много попыток. Вход временно заблокирован."
    return True, ""


def register_login_failure(ip: str, username: str) -> None:
    key = _attempt_key(ip, username)
    row = db.session.execute(
        select(LoginAttempt).where(LoginAttempt.key == key)
    ).scalar_one_or_none()
    now = now_local()
    if row is None:
        row = LoginAttempt(key=key, attempts=1, first_attempt=now, last_attempt=now)
        db.session.add(row)
    else:
        window = timedelta(minutes=int(current_app.config["LOGIN_WINDOW_MINUTES"]))
        if row.last_attempt and now - row.last_attempt > window:
            row.attempts = 1
            row.first_attempt = now
            row.locked_until = None
        else:
            row.attempts += 1
        row.last_attempt = now
        if row.attempts >= int(current_app.config["LOGIN_MAX_ATTEMPTS"]):
            row.locked_until = now + timedelta(
                minutes=int(current_app.config["LOGIN_LOCK_MINUTES"])
            )
    db.session.commit()


def clear_login_failures(ip: str, username: str) -> None:
    row = db.session.execute(
        select(LoginAttempt).where(LoginAttempt.key == _attempt_key(ip, username))
    ).scalar_one_or_none()
    if row:
        db.session.delete(row)
        db.session.commit()


def authenticate(username: str, password: str, ip: str) -> tuple[User | None, str]:
    username = clean_text(username, 120)
    password = password or ""
    allowed, message = check_login_allowed(ip, username)
    if not allowed:
        return None, message
    user = db.session.execute(
        select(User).where(User.username == username)
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        verify_password(_DUMMY_HASH, password)
        register_login_failure(ip, username)
        return None, "Неверный логин или пароль."
    if not verify_password(user.password_hash, password):
        register_login_failure(ip, username)
        return None, "Неверный логин или пароль."
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.last_login = now_local()
    db.session.commit()
    clear_login_failures(ip, username)
    return user, ""


def change_password(user: User, old_password: str, new_password: str) -> str | None:
    if not verify_password(user.password_hash, old_password):
        return "Текущий пароль указан неверно."
    strength_error = validate_password_strength(new_password)
    if strength_error:
        return strength_error
    user.password_hash = hash_password(new_password)
    db.session.commit()
    log_event("auth", f"Пароль администратора {user.username} изменён", "warning")
    return None


def admin_required(view: Callable):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            if request.path.startswith(current_app.config["ADMIN_PATH"]) and (
                request.is_json or request.path.endswith("/api/status")
            ):
                return jsonify({"ok": False, "error": "unauthorized"}), 401
            return redirect(url_for("admin.login", next=request.full_path))
        if session_expired():
            logout_user()
            if request.is_json:
                return jsonify({"ok": False, "error": "session_expired"}), 401
            return redirect(url_for("admin.login", expired=1))
        touch_session()
        return view(*args, **kwargs)

    return wrapped
