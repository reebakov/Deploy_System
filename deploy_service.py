"""Единый сервис запуска деплоев: общий lock, сборка команд, создание записей, работа с инвентарём.

Раньше эта логика была продублирована в bot.py и mm_server.py (и проверка «занято»
была подвержена гонке). Теперь проверка активного деплоя и создание записи в БД
выполняются атомарно под одним lock.
"""
import logging
import threading

from ansible_runner import _log_path
from config import PLAYBOOK_MOBIDESK, PLAYBOOK_OPENSSH
from database import DeployLog, get_active_log, session_scope
from inventory_manager import add_pcs_to_inventory, check_pcs_in_inventory
from settings_store import get_deploy_settings
from statuses import Status
from validation import ValidationError, parse_ansible_args, validate_tags

log = logging.getLogger(__name__)

# Один процесс uvicorn (workers=1): lock общий для бота и ручных запусков.
_start_lock = threading.Lock()

TASK_NAMES = {
    "full": "Полный деплой",
    "ssh": "Закинуть SSH",
    "room_only": "Обновление рума",
}


class DeployBusy(Exception):
    """Уже выполняется другой деплой."""


def build_command(deploy_type: str, pcs: list[str], skip_tags: str = "") -> tuple[str, list[str]]:
    """Собирает команду ansible-playbook. Все параметры проходят валидацию."""
    if deploy_type not in TASK_NAMES:
        raise ValidationError(f"Неизвестный тип деплоя: {deploy_type!r}")
    if not pcs:
        raise ValidationError("Не указаны ПК")

    target = ",".join(pcs)
    settings = get_deploy_settings()

    if deploy_type == "ssh":
        return TASK_NAMES[deploy_type], ["ansible-playbook", PLAYBOOK_OPENSSH, "-e", f"target={target}"]

    cmd = ["ansible-playbook", PLAYBOOK_MOBIDESK, "-e", f"target={target}"]
    if deploy_type == "full":
        skip_tags = validate_tags(skip_tags)
        if skip_tags:
            cmd += ["--skip-tags", skip_tags]
        else:
            cmd += parse_ansible_args(settings["full_args"])
    else:
        cmd += parse_ansible_args(settings["room_args"])

    if len(pcs) > 1:
        cmd += ["-f", str(len(pcs))]
    return TASK_NAMES[deploy_type], cmd


def create_deploy_log(*, jira_key: str, task_type: str, target_pcs: str, status: str,
                      deploy_type: str | None, skip_tags: str | None = None,
                      triggered_by: str | None = None, command: list[str] | None = None) -> int:
    """Атомарно: проверить отсутствие активного деплоя и создать запись. Бросает DeployBusy."""
    with _start_lock:
        with session_scope() as db:
            if get_active_log(db):
                raise DeployBusy("Уже выполняется другой деплой. Дождитесь его завершения.")
            entry = DeployLog(
                jira_key=jira_key, task_type=task_type, target_pcs=target_pcs, status=status,
                deploy_type=deploy_type, skip_tags=skip_tags, triggered_by=triggered_by,
                command=" ".join(command) if command else None,
            )
            db.add(entry)
            db.flush()
            entry.log_file = str(_log_path(entry.id))
            return entry.id


def update_log(log_id: int, status: str | None = None, target_pcs: str | None = None,
               command: list[str] | None = None, errors: str | None = None) -> None:
    with session_scope() as db:
        entry = db.query(DeployLog).filter(DeployLog.id == log_id).first()
        if not entry:
            return
        if status:
            entry.status = status
        if target_pcs:
            entry.target_pcs = target_pcs
        if command:
            entry.command = " ".join(command)
        if errors:
            entry.ansible_output = errors


def ensure_in_inventory(pcs: list[str], target_group: str | None = None) -> tuple[bool, list[str]]:
    """Добавляет отсутствующие ПК в инвентарь. Возвращает (успех, список реально добавленных)."""
    kwargs = {"target_group": target_group} if target_group else {}
    _, missing = check_pcs_in_inventory(pcs, **kwargs)
    if not missing:
        return True, []
    if not add_pcs_to_inventory(missing, **kwargs):
        return False, []
    return True, missing


def start_manual_deploy(*, deploy_type: str, pcs: list[str], skip_tags: str,
                        jira_key: str, task_suffix: str, user: str) -> tuple[int, list[str]]:
    """Ручной запуск/повтор: проверка → создание записи → (команда для фонового запуска).

    Возвращает (log_id, cmd). Бросает DeployBusy / ValidationError.
    """
    task_name, cmd = build_command(deploy_type, pcs, skip_tags)
    log_id = create_deploy_log(
        jira_key=jira_key, task_type=task_name + task_suffix, target_pcs=",".join(pcs),
        status=Status.RUNNING, deploy_type=deploy_type,
        skip_tags=skip_tags if deploy_type == "full" else None,
        triggered_by=user, command=cmd,
    )
    try:
        ok, _ = ensure_in_inventory(pcs)  # запись в БД уже сделана: параллельный деплой невозможен
    except Exception:
        log.exception("Inventory update failed")
        ok = False
    if not ok:
        update_log(log_id, Status.INV_ERROR)
        raise RuntimeError("Ошибка при записи недостающих ПК в файл инвентаря.")
    return log_id, cmd
