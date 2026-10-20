import logging
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from config import ANSIBLE_CWD, ANSIBLE_TIMEOUT, DEPLOY_LOG_DIR
from pc_checks import check_ssh_many
from database import DeployLog, session_scope
from statuses import Status, is_active

log = logging.getLogger(__name__)

ANSIBLE_RC = {
    0:   "Успешно",
    1:   "Ошибка выполнения (1)",
    2:   "Машина(ы) failed (2)",
    3:   "Машина(ы) failed и unreachable (3)",
    # В ansible-playbook код возврата — битовая маска: 1 ошибка, 2 failed-хосты, 4 unreachable-хосты,
    # 8 прерван break_play. Поэтому 4 — это НЕДОСТУПНЫЙ ПК, а не «ошибка парсинга плейбука».
    4:   "ПК недоступен (unreachable) (4)",
    5:   "Ошибка + ПК недоступен (5)",
    6:   "Машина(ы) failed и unreachable (6)",
    7:   "Ошибка, failed и unreachable (7)",
    8:   "Плейбук остановлен (break_play) (8)",
    -15: "Прервано пользователем (SIGTERM)",
    -9:  "Прервано пользователем (SIGKILL)",
    99:  "Прервано пользователем (99)",
    100: "Плейбук завершён с ошибками (100)",
    250: "Неожиданная ошибка Ansible (250)",
}

# Коды, при которых имеет смысл разбирать PLAY RECAP (failed / unreachable хосты)
RECAP_CODES = (2, 3, 4, 5, 6, 7)
ERROR_LINE_RE = re.compile(r"^(fatal:|FAILED!|.*UNREACHABLE!|ERROR!)", re.IGNORECASE)

_HAS_PGROUP = hasattr(os, "killpg")


def get_ansible_status(code: int) -> str:
    return ANSIBLE_RC.get(code, f"Неизвестный код {code}")


def parse_recap(content: str) -> tuple[int, int] | None:
    """Возвращает (успешных хостов, проблемных хостов) по блоку PLAY RECAP или None."""
    if "PLAY RECAP" not in content:
        return None
    recap_block = content.split("PLAY RECAP")[-1]
    success_hosts = failed_hosts = 0
    for line in recap_block.splitlines():
        line = line.strip()
        if not line or ":" not in line or "ok=" not in line:
            continue
        if re.search(r'failed=[1-9]\d*', line) or re.search(r'unreachable=[1-9]\d*', line):
            failed_hosts += 1
        elif re.search(r'ok=[1-9]\d*', line):
            success_hosts += 1
    return success_hosts, failed_hosts


def failed_hosts(content: str) -> list[str]:
    """Хосты с failed/unreachable по последнему PLAY RECAP (localhost не учитывается)."""
    if "PLAY RECAP" not in content:
        return []
    result: list[str] = []
    for line in content.split("PLAY RECAP")[-1].splitlines():
        line = line.strip()
        if ":" not in line or "ok=" not in line:
            continue
        host = line.split(":", 1)[0].strip()
        if host != "localhost" and (re.search(r'failed=[1-9]\d*', line)
                                    or re.search(r'unreachable=[1-9]\d*', line)):
            result.append(host)
    return result


def failed_hosts_from_log(log_file: Path | str | None) -> list[str]:
    if not log_file or not Path(log_file).exists():
        return []
    try:
        return failed_hosts(Path(log_file).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []


def get_detailed_status(rc: int, log_file: Path | None) -> str:
    base_status = get_ansible_status(rc)
    if rc not in RECAP_CODES or not log_file or not Path(log_file).exists():
        return base_status
    try:
        recap = parse_recap(Path(log_file).read_text(encoding="utf-8", errors="replace"))
        if recap:
            ok, failed = recap
            if ok > 0 and failed > 0:
                return f"Частично ({ok} ок, {failed} fail)"
            if ok == 0 and failed > 0:
                return f"Все failed ({failed} шт)"
    except Exception as e:
        log.warning("Ошибка парсинга лога для статуса: %s", e)
    return base_status


def summarize_errors(log_file: Path | str | None, limit: int = 5, width: int = 300) -> str:
    """Выжимка причин сбоя из лога (строки fatal / UNREACHABLE / ERROR!) для уведомлений."""
    if not log_file or not Path(log_file).exists():
        return ""
    try:
        text = Path(log_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    found: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if ERROR_LINE_RE.match(line) and line not in found:
            found.append(line[:width])
    return "\n".join(found[-limit:])


def _log_path(log_id: int) -> Path:
    return DEPLOY_LOG_DIR / f"deploy_{log_id}.log"


def _save_result(log_id: int, status: str) -> None:
    with session_scope() as db:
        dblog = db.query(DeployLog).filter(DeployLog.id == log_id).first()
        if dblog:
            dblog.status = status


def _save_errors(log_id: int, errors: str) -> None:
    if not errors:
        return
    with session_scope() as db:
        dblog = db.query(DeployLog).filter(DeployLog.id == log_id).first()
        if dblog:
            dblog.ansible_output = errors


def _save_pid(log_id: int, pid: int, log_file: Path | None) -> None:
    with session_scope() as db:
        dblog = db.query(DeployLog).filter(DeployLog.id == log_id).first()
        if dblog:
            dblog.pid = pid
            if log_file:
                dblog.log_file = str(log_file)


def _signal_group(pid: int, sig: int) -> None:
    if _HAS_PGROUP:
        os.killpg(os.getpgid(pid), sig)
    else:  # Windows (разработка)
        os.kill(pid, signal.SIGTERM)


def _terminate(proc: subprocess.Popen, grace: int = 5) -> None:
    """SIGTERM на группу процессов, через grace секунд — SIGKILL."""
    try:
        _signal_group(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.2)
    try:
        _signal_group(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except ProcessLookupError:
        pass


def execute(ansible_cmd: list[str], log_id: int | None = None,
            timeout: int = ANSIBLE_TIMEOUT) -> tuple[int, str, str]:
    """Единая точка запуска ansible. Возвращает (rc, stdout_если_нет_лог_файла, статус).

    Если передан log_id — вывод дописывается в файл лога, pid сохраняется в БД.
    Таймаут всегда даёт статус «Таймаут» (rc = -1).
    """
    log_file = _log_path(log_id) if log_id else None
    try:
        sink = open(log_file, "a", encoding="utf-8") if log_file else None
    except OSError as e:
        return -1, f"Ошибка открытия лога: {e}", Status.CRITICAL

    try:
        if sink:
            sink.write(f"\n🚀 Command: {' '.join(ansible_cmd)}\n=== ANSIBLE LOGS ===\n")
            sink.flush()
        proc = subprocess.Popen(
            ansible_cmd,
            cwd=str(ANSIBLE_CWD),
            stdout=sink if sink else subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        if log_id:
            _save_pid(log_id, proc.pid, log_file)

        try:
            if sink:
                rc, out = proc.wait(timeout=timeout), ""
            else:
                out, _ = proc.communicate(timeout=timeout)
                rc = proc.returncode
        except subprocess.TimeoutExpired:
            _terminate(proc)
            out = "" if sink else (proc.communicate()[0] or "")
            return -1, out, Status.TIMEOUT
    except Exception as e:
        log.exception("Ansible run failed")
        return -1, f"Ошибка запуска: {e}", Status.CRITICAL
    finally:
        if sink:
            sink.close()

    status = get_detailed_status(rc, log_file) if log_file else get_ansible_status(rc)
    return rc, (out or ""), status


def run_ansible_blocking(ansible_cmd: list[str], log_id: int | None = None,
                         timeout: int = ANSIBLE_TIMEOUT) -> tuple[int, str, str]:
    """Синхронный запуск; статус в БД не пишется (это делает вызывающий код)."""
    return execute(ansible_cmd, log_id=log_id, timeout=timeout)


def preflight_unreachable(pcs: list[str], deploy_type: str | None, write) -> bool:
    """Быстрая проверка порта до запуска Ansible. True — недоступны ВСЕ ПК (запускать нет смысла).

    Для SSH-init проверяется WinRM (5985), для остальных деплоев — SSH (22).
    Иначе Ansible висит 30+ секунд на таймауте и заканчивается невнятным rc=4.
    """
    if not pcs:
        return False
    port = 5985 if deploy_type == "ssh" else 22
    results = check_ssh_many(pcs, port=port)
    closed = [p for p in pcs if not results.get(p)]
    for p in pcs:
        write(f"Проверка порта {port} на {p}: {'открыт' if results.get(p) else 'ЗАКРЫТ'}")
    if closed and len(closed) < len(pcs):
        write(f"⚠️ Недоступны: {', '.join(closed)} — Ansible покажет для них unreachable.")
    return len(closed) == len(pcs)


def run_and_wait_detached(ansible_cmd: list[str], log_id: int, timeout: int = ANSIBLE_TIMEOUT,
                          pcs: list[str] | None = None, deploy_type: str | None = None) -> None:
    """Запуск для ручных деплоев: результат (в т.ч. таймаут) сразу пишется в БД."""
    log_file = _log_path(log_id)
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"Command: {' '.join(ansible_cmd)}\n\n=== LOGS ===\n")

            def write(msg: str) -> None:
                f.write(msg + "\n")
                f.flush()

            if pcs and preflight_unreachable(pcs, deploy_type, write):
                write("❌ Все выбранные ПК недоступны по сети — запуск Ansible отменён.")
                unreachable = True
            else:
                unreachable = False
    except OSError as e:
        _save_result(log_id, f"{Status.CRITICAL}: {e}")
        return
    if unreachable:
        _save_result(log_id, Status.UNREACHABLE)
        return
    rc, _, status = execute(ansible_cmd, log_id=log_id, timeout=timeout)
    _save_result(log_id, status)
    if rc != 0:
        _save_errors(log_id, summarize_errors(log_file))


def pid_alive(pid: int | None) -> bool:
    if not pid or not _HAS_PGROUP:  # на Windows os.kill(pid, 0) убивает процесс — не используем
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def cancel_deploy(log_id: int) -> bool:
    """Отменяет только АКТИВНЫЙ деплой (защита от убийства чужого процесса по переиспользованному pid)."""
    with session_scope() as db:
        dblog = db.query(DeployLog).filter(DeployLog.id == log_id).first()
        if not dblog or not dblog.pid or not is_active(dblog.status):
            return False
        pid = dblog.pid
    try:
        _signal_group(pid, signal.SIGTERM)
        log.info("Cancelled deploy #%s (pid=%s)", log_id, pid)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _infer_status_from_log(log_file: str | None) -> str:
    if log_file and os.path.exists(log_file):
        try:
            recap = parse_recap(Path(log_file).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            recap = None
        if recap:
            ok, failed = recap
            if failed == 0 and ok > 0:
                return "Успешно (подтверждено по логу после рестарта)"
            if ok > 0:
                return f"Частично ({ok} ок, {failed} fail)"
            return f"Все failed ({failed} шт)"
    return Status.INTERRUPTED


def _watch_orphan(log_id: int, pid: int, log_file: str | None) -> None:
    while pid_alive(pid):
        time.sleep(5)
    _save_result(log_id, _infer_status_from_log(log_file))
    log.info("Orphan deploy #%s (pid=%s) finished", log_id, pid)


def recover_active_deploys() -> None:
    """При старте сервиса: живые процессы остаются «активными» (за ними следит watcher),
    мёртвые получают статус по логу — вместо слепого «Прервано (рестарт)»."""
    with session_scope() as db:
        active = [(d.id, d.pid, d.log_file) for d in db.query(DeployLog).all() if is_active(d.status)]
    for log_id, pid, log_file in active:
        if pid_alive(pid):
            log.warning("Deploy #%s (pid=%s) still running after restart — watching", log_id, pid)
            threading.Thread(target=_watch_orphan, args=(log_id, pid, log_file),
                             daemon=True, name=f"orphan-{log_id}").start()
        else:
            _save_result(log_id, _infer_status_from_log(log_file) if pid else Status.INTERRUPTED)
