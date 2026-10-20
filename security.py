"""Аутентификация дашборда: Basic Auth + защита от брутфорса + CSRF-токены."""
import hashlib
import hmac
import secrets
import threading
import time

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import config

security = HTTPBasic(auto_error=False)

# Секрет генерируется при старте, если не задан в окружении
_csrf_secret = (config.CSRF_SECRET or secrets.token_hex(32)).encode()

_fail_lock = threading.Lock()
_failures: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _is_locked(ip: str) -> bool:
    now = time.monotonic()
    with _fail_lock:
        recent = [t for t in _failures.get(ip, []) if now - t < config.AUTH_LOCKOUT_SECONDS]
        _failures[ip] = recent
        return len(recent) >= config.AUTH_MAX_FAILURES


def _register_failure(ip: str) -> None:
    with _fail_lock:
        _failures.setdefault(ip, []).append(time.monotonic())


def reset_rate_limits() -> None:
    with _fail_lock:
        _failures.clear()


def check_auth(request: Request, creds: HTTPBasicCredentials | None = Depends(security)) -> str:
    ip = _client_ip(request)
    if _is_locked(ip):
        raise HTTPException(status_code=429, detail="Слишком много неудачных попыток входа. Попробуйте позже.")

    if creds is not None:
        # compare_digest по bytes: не падает на не-ASCII символах в логине/пароле
        ok_user = secrets.compare_digest(creds.username.encode(), config.DASH_USER.encode())
        ok_pass = secrets.compare_digest(creds.password.encode(), config.DASH_PASS.encode())
        if ok_user and ok_pass:
            return creds.username
        _register_failure(ip)

    raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})


def make_csrf_token(user: str) -> str:
    return hmac.new(_csrf_secret, user.encode(), hashlib.sha256).hexdigest()


async def csrf_protect(request: Request, user: str = Depends(check_auth)) -> str:
    """Для всех изменяющих запросов: токен в заголовке X-CSRF-Token или в поле формы csrf_token."""
    token = request.headers.get("x-csrf-token")
    if not token and "form" in (request.headers.get("content-type") or ""):
        form = await request.form()
        token = form.get("csrf_token")
    if not token or not hmac.compare_digest(str(token), make_csrf_token(user)):
        raise HTTPException(status_code=403, detail="Неверный CSRF-токен. Обновите страницу.")
    return user
