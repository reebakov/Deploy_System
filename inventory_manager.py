import fcntl
import logging
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from config import INVENTORY_PATH, INVENTORY_LOCK_PATH, TARGET_GROUP

log = logging.getLogger(__name__)


@contextmanager
def _inventory_lock():
    INVENTORY_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(INVENTORY_LOCK_PATH, 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def parse_inventory(path: str | Path | None = None) -> dict[str, list[str]]:
    p = Path(path or INVENTORY_PATH)
    if not p.exists():
        return {}
    groups: dict[str, list[str]] = {}
    current: str | None = None
    for raw in p.read_text().splitlines():
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
    hosts_upper = {h.upper() for h in _get_group_hosts(groups, target_group)}

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
        with os.fdopen(fd, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def add_pcs_to_inventory(pc_list: list[str], target_group: str = TARGET_GROUP) -> bool:
    if not INVENTORY_PATH.exists():
        log.error("Inventory file %s not found", INVENTORY_PATH)
        return False

    with _inventory_lock():
        lines = INVENTORY_PATH.read_text().splitlines()

        in_target   = False
        found_group = False
        group_start = -1
        group_end   = -1
        current_hosts: list[str] = []

        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith('['):
                if in_target:
                    group_end = i
                    break
                if s.lower() == target_group.lower():
                    in_target = True
                    found_group = True
                    group_start = i
            elif in_target and s and not s.startswith('#'):
                current_hosts.append(s)
        if in_target and group_end == -1:
            group_end = len(lines)

        existing_upper = {h.upper() for h in current_hosts}
        missing = [pc.upper() for pc in pc_list if pc.upper() not in existing_upper]
        if not missing:
            log.info("All PCs already in group %s", target_group)
            return True

        all_hosts = current_hosts + missing
        all_hosts.sort(key=natural_sort_key)

        if found_group:
            new_lines = lines[:group_start + 1] + all_hosts + lines[group_end:]
        else:
            log.warning("Group %s not found, appending at the end", target_group)
            new_lines = lines + ['', target_group] + all_hosts

        if INVENTORY_PATH.exists():
            backup_path = INVENTORY_PATH.with_suffix('.bak')
            try:
                shutil.copy2(INVENTORY_PATH, backup_path)
            except Exception as e:
                log.error("Не удалось создать бэкап инвентаря: %s", e)

        _write_atomic(INVENTORY_PATH, new_lines)
        log.info("Added PCs: %s", ', '.join(missing))
        return True