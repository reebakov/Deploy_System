import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from validation import PC_NAME_RE


def check_ping(hostname: str, timeout: int = 1) -> bool:
    # hostname приходит из Insight/Jira — не даём ему стать опцией ping
    if hostname.startswith('-'):
        return False
    if sys.platform.startswith('win'):
        cmd = ["ping", "-n", "1", "-w", str(timeout * 1000), hostname]
    else:
        cmd = ["ping", "-c", "1", "-W", str(timeout), hostname]
    try:
        r = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 2,
        )
        return r.returncode == 0
    except Exception:
        return False


def check_ssh(hostname: str, port: int = 22, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((hostname, port), timeout=timeout):
            return True
    except Exception:
        return False


def _check_many(func, hosts: list[str], workers: int, **kwargs) -> dict[str, bool]:
    if not hosts:
        return {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda h: func(h, **kwargs), hosts))
    return dict(zip(hosts, results))


def check_ping_many(hosts: list[str], workers: int = 16) -> dict[str, bool]:
    return _check_many(check_ping, hosts, workers)


def check_ssh_many(hosts: list[str], workers: int = 16, port: int = 22) -> dict[str, bool]:
    return _check_many(check_ssh, hosts, workers, port=port)


def is_valid_pc_name(name: str) -> bool:
    return bool(PC_NAME_RE.match(name or ''))
