import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor


def check_ping(hostname: str, timeout: int = 1) -> bool:
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), hostname],
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


def check_ping_many(hosts: list[str], workers: int = 16) -> dict[str, bool]:
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(check_ping, hosts))
    return dict(zip(hosts, results))


def check_ssh_many(hosts: list[str], workers: int = 16) -> dict[str, bool]:
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(check_ssh, hosts))
    return dict(zip(hosts, results))