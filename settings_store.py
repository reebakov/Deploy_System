"""Хранилище настроек деплоя и флага автобота (атомарная запись, валидация)."""
import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from config import ROOM_TAGS_DEFAULT, SKIP_TAGS_DEFAULT, STATE_DIR
from validation import parse_ansible_args

log = logging.getLogger(__name__)
_lock = threading.Lock()

DEFAULT_SETTINGS = {
    "full_args": f"--skip-tags {SKIP_TAGS_DEFAULT}",
    "room_args": f"--tags {ROOM_TAGS_DEFAULT}",
}


def _settings_file() -> Path:
    return STATE_DIR / "deploy_settings.json"


def _bot_flag_file() -> Path:
    return STATE_DIR / "bot_status.txt"


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_deploy_settings() -> dict:
    """Настройки из файла; при ошибке или невалидных аргументах — значения по умолчанию."""
    settings = dict(DEFAULT_SETTINGS)
    path = _settings_file()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for key in DEFAULT_SETTINGS:
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    parse_ansible_args(value)  # защита и от ручной правки файла
                    settings[key] = value.strip()
        except Exception as e:
            log.warning("Failed to read %s: %s", path, e)
    return settings


def save_deploy_settings(full_args: str, room_args: str) -> None:
    """Сохраняет настройки. Бросает ValidationError, если аргументы не из белого списка."""
    parse_ansible_args(full_args)
    parse_ansible_args(room_args)
    payload = {"full_args": full_args.strip(), "room_args": room_args.strip()}
    with _lock:
        _write_atomic(_settings_file(), json.dumps(payload, ensure_ascii=False, indent=2))


def is_bot_enabled() -> bool:
    path = _bot_flag_file()
    try:
        return path.exists() and path.read_text().strip() == "1"
    except OSError:
        return False


def set_bot_enabled(enabled: bool) -> None:
    with _lock:
        _write_atomic(_bot_flag_file(), "1" if enabled else "0")
