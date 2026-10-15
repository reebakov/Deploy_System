import logging
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from config import ANSIBLE_CWD, ANSIBLE_TIMEOUT, DEPLOY_LOG_DIR
from database import SessionLocal, DeployLog

log = logging.getLogger(__name__)

ANSIBLE_RC = {
    0:   "Успешно",
    1:   "Ошибка выполнения (1)",
    2:   "Машина(ы) failed (2)",
    3:   "Машина(ы) unreachable (3)",
    4:   "Ошибка парсинга плейбука (4)",
    5:   "Неверные опции CLI (5)",
    -15: "Прервано пользователем (SIGTERM)",
    -9:  "Прервано пользователем (SIGKILL)",
    99:  "Прервано пользователем (99)",
    100: "Плейбук завершён с ошибками (100)",
}

def get_ansible_status(code: int) -> str:
    return ANSIBLE_RC.get(code, f"Неизвестный код {code}")

def get_detailed_status(rc: int, log_file: Path) -> str:
    base_status = get_ansible_status(rc)
    if rc not in (2, 3) or not log_file or not log_file.exists():
        return base_status
    
    try:
        content = log_file.read_text(errors="replace")
        if "PLAY RECAP" not in content:
            return base_status
        
        recap_block = content.split("PLAY RECAP")[-1]
        
        success_hosts = 0
        failed_hosts = 0
        
        for line in recap_block.splitlines():
            line = line.strip()
            if not line or ":" not in line or "ok=" not in line:
                continue
                
            if re.search(r'failed=[1-9]\d*', line) or re.search(r'unreachable=[1-9]\d*', line):
                failed_hosts += 1
            elif re.search(r'ok=[1-9]\d*', line):
                success_hosts += 1
                
        if success_hosts > 0 and failed_hosts > 0:
            return f"Частично ({success_hosts} ок, {failed_hosts} fail)"
        elif success_hosts == 0 and failed_hosts > 0:
            return f"Все failed ({failed_hosts} шт)"
    except Exception as e:
        log.warning("Ошибка парсинга лога для статуса: %s", e)
        
    return base_status

def _log_path(log_id: int) -> Path:
    return DEPLOY_LOG_DIR / f"deploy_{log_id}.log"

def _save_result(log_id: int, status: str):
    db = SessionLocal()
    try:
        dblog = db.query(DeployLog).filter(DeployLog.id == log_id).first()
        if dblog:
            dblog.status = status
            db.commit()
    finally:
        db.close()

def run_and_wait_detached(ansible_cmd: list[str], log_id: int, timeout: int = ANSIBLE_TIMEOUT):
    log_file = _log_path(log_id)
    try:
        f = open(log_file, "w")
        f.write(f"Command: {' '.join(ansible_cmd)}\n\n=== LOGS ===\n")
        f.flush()
    except Exception as e:
        _save_result(log_id, f"Критическая ошибка: {e}")
        return

    try:
        proc = subprocess.Popen(
            ansible_cmd,
            cwd=str(ANSIBLE_CWD),
            stdout=f,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        f.close()

        db = SessionLocal()
        try:
            dblog = db.query(DeployLog).filter(DeployLog.id == log_id).first()
            if dblog:
                dblog.pid = proc.pid
                dblog.log_file = str(log_file)
                db.commit()
        finally:
            db.close()

        try:
            rc = proc.wait(timeout=timeout)
            status = get_detailed_status(rc, log_file)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                time.sleep(5)
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            status = "Таймаут"

        _save_result(log_id, status)

    except Exception as e:
        log.exception("Ansible run failed")
        _save_result(log_id, "Критическая ошибка")

def run_ansible_blocking(ansible_cmd: list[str], log_id: int = None, timeout: int = ANSIBLE_TIMEOUT) -> tuple[int, str, str]:
    log_file = _log_path(log_id) if log_id else None
    f = open(log_file, "a") if log_file else None
    
    if f:
        f.write(f"\n🚀 Command: {' '.join(ansible_cmd)}\n=== ANSIBLE LOGS ===\n")
        f.flush()

    try:
        proc = subprocess.Popen(
            ansible_cmd,
            cwd=str(ANSIBLE_CWD),
            stdout=f if f else subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True 
        )
        
        if log_id:
            db = SessionLocal()
            try:
                dblog = db.query(DeployLog).filter(DeployLog.id == log_id).first()
                if dblog:
                    dblog.pid = proc.pid
                    db.commit()
            finally:
                db.close()

        try:
            if f:
                rc = proc.wait(timeout=timeout)
                out = ""
            else:
                out, _ = proc.communicate(timeout=timeout)
                rc = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            if not f:
                out, _ = proc.communicate()
            else:
                out = ""
            return -1, (out or ""), "Таймаут"
        finally:
            if f:
                f.close()
        
        status = get_detailed_status(rc, log_file) if log_file else get_ansible_status(rc)
        return rc, (out or ""), status
    except Exception as e:
        if f:
            f.close()
        return -1, f"Ошибка запуска: {e}", "Критическая ошибка"

def cancel_deploy(log_id: int) -> bool:
    db = SessionLocal()
    try:
        dblog = db.query(DeployLog).filter(DeployLog.id == log_id).first()
        if not dblog or not dblog.pid:
            return False
        try:
            os.killpg(os.getpgid(dblog.pid), signal.SIGTERM)
            log.info("Cancelled deploy #%s (pid=%s)", log_id, dblog.pid)
            return True
        except (ProcessLookupError, PermissionError):
            return False
    finally:
        db.close()