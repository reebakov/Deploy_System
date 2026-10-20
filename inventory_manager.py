import logging
import os
import re
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from config import INVENTORY_LOCK_PATH, INVENTORY_PATH, TARGET_GROUP

try:
    import fcntl
except ImportError:  # Windows: разработка/тесты; на боевом Linux-сервере используется flock
    fcntl = None

log = logging.getLogger(__name__)

_thread_lock = threading.RLock()


@contextmanager
def _inventory_lock():
    """Межпроцессная (flock) + межпоточная блокировка инвентаря."""
    with _thread_lock:
        if fcntl is None:
            yield
            return
        INVENTORY_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(INVENTORY_LOCK_PATH, 'w') as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)


def _host_name(line: str) -> str:
    """Имя хоста — первый токен строки (строка может содержать host-vars: 'A105 ansible_host=...')."""
    parts = line.split()
    return parts[0] if parts else ''


def parse_inventory(path: str | Path | None = None) -> dict[str, list[str]]:
    p = Path(path or INVENTORY_PATH)
    if not p.exists():
        return {}
    groups: dict[str, list[str]] = {}
    current: str | None = None
    for raw in p.read_text(encoding='utf-8').splitlines():
        s = raw.strip()
        if not s or s.startswith('#'):
            continue
        if s.startswith('[') and s.endswith(']'):
            current = s
            groups.setdefault(current, [])
        elif current is not None:
            groups[current].append(s)
    return groups


def _get_group_hosts(groups: dict[str, list[str]], target_group: str) -> list[str]:
    for g, hosts in groups.items():
        if g.lower() == target_group.lower():
            return hosts
    return []


def check_pcs_in_inventory(pc_list: list[str], target_group: str = TARGET_GROUP):
    if not INVENTORY_PATH.exists():
        log.error("Inventory file %s not found", INVENTORY_PATH)
        return [], list(pc_list)

    groups = parse_inventory()
    hosts_upper = {_host_name(h).upper() for h in _get_group_hosts(groups, target_group)}

    found, missing = [], []
    for pc in pc_list:
        (found if pc.upper() in hosts_upper else missing).append(pc)
    return found, missing


def natural_sort_key(host: str):
    parts = re.split(r'(\d+)', host)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def _write_atomic(path: Path, lines: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.inv.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _locate_group(lines: list[str], target_group: str) -> tuple[int, int]:
    """Возвращает (start, end): индекс заголовка группы и индекс следующего заголовка/конца файла."""
    start = -1
    for i, line in enumerate(lines):
        s = line.strip()
        if not s.startswith('['):
            continue
        if start != -1:
            return start, i
        if s.lower() == target_group.lower():
            start = i
    return (start, len(lines)) if start != -1 else (-1, -1)


def _rebuild_group(lines: list[str], start: int, end: int, hosts: list[str]) -> list[str]:
    """Пересобирает тело группы: комментарии сохраняются, хосты сортируются, хвостовые пустые строки остаются."""
    body = lines[start + 1:end]
    comments = [ln for ln in body if ln.strip().startswith('#')]
    trailing_blank = 0
    for ln in reversed(body):
        if ln.strip():
            break
        trailing_blank += 1
    sorted_hosts = sorted(hosts, key=lambda h: natural_sort_key(_host_name(h)))
    return lines[:start + 1] + comments + sorted_hosts + [''] * trailing_blank + lines[end:]


def _read_group(lines: list[str], start: int, end: int) -> list[str]:
    return [ln.strip() for ln in lines[start + 1:end]
            if ln.strip() and not ln.strip().startswith('#')]


def _backup(path: Path) -> None:
    try:
        shutil.copy2(path, path.with_suffix('.bak'))
    except Exception as e:
        log.error("Не удалось создать бэкап инвентаря: %s", e)


def add_pcs_to_inventory(pc_list: list[str], target_group: str = TARGET_GROUP) -> bool:
    if not INVENTORY_PATH.exists():
        log.error("Inventory file %s not found", INVENTORY_PATH)
        return False

    with _inventory_lock():
        lines = INVENTORY_PATH.read_text(encoding='utf-8').splitlines()
        start, end = _locate_group(lines, target_group)

        current_hosts = _read_group(lines, start, end) if start != -1 else []
        existing_upper = {_host_name(h).upper() for h in current_hosts}
        missing = [pc.upper() for pc in dict.fromkeys(pc_list) if pc.upper() not in existing_upper]
        if not missing:
            log.info("All PCs already in group %s", target_group)
            return True

        if start != -1:
            new_lines = _rebuild_group(lines, start, end, current_hosts + missing)
        else:
            log.warning("Group %s not found, appending at the end", target_group)
            new_lines = lines + ['', target_group] + sorted(missing, key=natural_sort_key)

        _backup(INVENTORY_PATH)
        _write_atomic(INVENTORY_PATH, new_lines)
        log.info("Added PCs: %s", ', '.join(missing))
        return True


def remove_pcs_from_inventory(pc_list: list[str], target_group: str = TARGET_GROUP) -> bool:
    """Удаляет ПК из группы (откат после неудачного деплоя)."""
    if not pc_list or not INVENTORY_PATH.exists():
        return True

    with _inventory_lock():
        lines = INVENTORY_PATH.read_text(encoding='utf-8').splitlines()
        start, end = _locate_group(lines, target_group)
        if start == -1:
            return True

        to_remove = {pc.upper() for pc in pc_list}
        current_hosts = _read_group(lines, start, end)
        kept = [h for h in current_hosts if _host_name(h).upper() not in to_remove]
        if len(kept) == len(current_hosts):
            return True

        _backup(INVENTORY_PATH)
        _write_atomic(INVENTORY_PATH, _rebuild_group(lines, start, end, kept))
        log.info("Removed PCs from inventory: %s", ', '.join(sorted(to_remove)))
        return True
