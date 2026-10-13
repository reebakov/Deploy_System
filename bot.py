import logging
import re
from ansible_runner import run_ansible_blocking, _log_path
from config import (
    ACCOUNTS_FIELD_ID, EPIC_FIELD_ID,
    PLAYBOOK_MOBIDESK, PLAYBOOK_OPENSSH, POKER_FIELD_ID,
    PROBLEM_DESC_FIELD_ID, ROLE_FIELD_ID, SKIP_TAGS_DEFAULT,
    STATE_FIELD_ID, TARGET_GROUP, JIRA_USER,  # ДОБАВЛЕН JIRA_USER
)
from database import DeployLog, SessionLocal
from inventory_manager import add_pcs_to_inventory, check_pcs_in_inventory
from jira_clients import get_jira, insight_search, insight_update_pc, insight_revert_pc
from logging_setup import setup_logging
from notify import send_mattermost
from pc_checks import check_ping_many, check_ssh_many

log = setup_logging()

IQL_FREE_PC = (
    'objectType = "Automat" AND Status = "Ожидает" AND '
    'Type IN ("Host Emulator", "Physical") AND '
    '"Видеокарта" IN ("Intel HD Graphics 630", "NVIDIA GeForce GT 1030")'
)

PC_PATTERN = re.compile(r'\b[AaАа]\d+\b')


def classify_issue(issue):
    desc = (issue.fields.description or "").lower()
    epic_val = str(getattr(issue.fields, EPIC_FIELD_ID, '') or '').upper()
    poker_val = str(getattr(issue.fields, POKER_FIELD_ID, '') or '').upper()
    role_val = str(getattr(issue.fields, ROLE_FIELD_ID, '') or '').upper()
    state_val = str(getattr(issue.fields, STATE_FIELD_ID, '') or '').lower()
    problem_desc = str(getattr(issue.fields, PROBLEM_DESC_FIELD_ID, '') or '').lower()
    full_desc = f"{desc} {problem_desc}"

    if not ("CLUBGG" in poker_val and "HMS NSK" in role_val):
        return None, None, full_desc
    if not ("DR-401" in epic_val or "DR-390" in epic_val):
        return None, None, full_desc

    if "комп есть, требуется подготовка без интернета" in state_val:
        return "A", "Деплой ПК (уже есть)", full_desc
    if "нет в гр" in full_desc or "не отображается в гр" in full_desc:
        return "B", "Доработка (нет в ГР)", full_desc
    if "комп новый, требуется подбор" in state_val:
        return "C", "Деплой новых ПК", full_desc
    return None, None, full_desc


def extract_pcs(text: str) -> list[str]:
    return sorted({m.upper() for m in PC_PATTERN.findall(text)})


def ensure_in_inventory(pcs: list[str], L) -> bool:
    found, missing = check_pcs_in_inventory(pcs)
    if not missing:
        L("✅ Все ПК найдены в инвентаре.")
        return True
    L(f"⚠️ ПК {', '.join(missing)} отсутствуют в группе {TARGET_GROUP}.")
    L(f"⚡ Автоматически добавляем ПК {', '.join(missing)} в инвентарь...")
    if not add_pcs_to_inventory(missing, TARGET_GROUP):
        L("❌ Ошибка записи в файл инвентаря.")
        return False
    L(f"✅ ПК {', '.join(missing)} добавлены в инвентарь.")
    return True


def pick_free_pcs(need: int, L) -> list[dict]:
    L(f"🔍 Требуется подобрать ПК: {need} шт. Идем в Insight...")
    try:
        objects = insight_search(IQL_FREE_PC, result_per_page=50)
    except Exception as e:
        L(f"❌ Ошибка получения данных из Insight: {e}")
        return []

    candidates = [o for o in objects if o.get("label")]
    ping_results = check_ping_many([c["label"] for c in candidates])

    selected: list[dict] = []
    for obj in candidates:
        if len(selected) >= need:
            break
        name = obj["label"]
        if ping_results.get(name):
            L(f"✅ ПК {name} доступен. Бронируем...")
            if insight_update_pc(obj["id"]):
                selected.append({"name": name, "id": obj["id"]})
                L(f"   └ Статус {name} обновлён в Insight")
            else:
                L(f"⚠️ Не удалось обновить статус {name} в Insight.")
        else:
            L(f"❌ ПК {name} не пингуется, ищем дальше.")
    return selected


def run_ssh_init_if_needed(pcs: list[str], L):
    ssh_results = check_ssh_many(pcs)
    for p in pcs:
        L(f"SSH на {p}: {'✅ открыт' if ssh_results.get(p) else '⚠️ закрыт'}")

    need_init = [p for p in pcs if not ssh_results.get(p)]
    if not need_init:
        return

    cmd = ["ansible-playbook", PLAYBOOK_OPENSSH, "-e", f"target={','.join(need_init)}"]
    L(f"🚀 Запускаем SSH init: {' '.join(cmd)}")
    
    rc, out, status = run_ansible_blocking(cmd)
    L(f"SSH init rc={rc} ({status})\n{out}")
    if rc != 0:
        L("⚠️ Ошибка SSH init. Продолжаем на свой страх и риск.")


def build_ansible_cmd(scenario: str, target_pcs: str, pc_count: int) -> list[str]:
    if scenario == "C":
        cmd = ["ansible-playbook", PLAYBOOK_MOBIDESK,
               "-e", f"target={target_pcs}",
               "--skip-tags", SKIP_TAGS_DEFAULT]
    else:
        cmd = ["ansible-playbook", PLAYBOOK_MOBIDESK,
               "-e", f"target={target_pcs}",
               "--tags", "neoagent,clubgg"]
    if pc_count > 1:
        cmd += ["-f", str(pc_count)]
    return cmd


def _update_db_status(log_id: int, status: str, target_pcs: str = None):
    db = SessionLocal()
    try:
        dblog = db.query(DeployLog).filter(DeployLog.id == log_id).first()
        if dblog:
            dblog.status = status
            if target_pcs:
                dblog.target_pcs = target_pcs
            db.commit()
    finally:
        db.close()


def process_issue(jira, issue) -> bool:
    scenario, task_type, full_desc = classify_issue(issue)
    if not scenario:
        return False

    db = SessionLocal()
    new_log = DeployLog(
        jira_key=issue.key,
        task_type=task_type,
        target_pcs="Подбор ПК...",
        status="Инициализация"
    )
    db.add(new_log); db.commit(); db.refresh(new_log)
    log_id = new_log.id
    log_file = str(_log_path(log_id))
    new_log.log_file = log_file
    db.commit(); db.close()

    def L(msg: str):
        log.info("[%s] %s", issue.key, msg)
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    L(f"--- Задача [{issue.key}] {issue.fields.summary} — {task_type} ---")

    selected_pcs_dicts = []
    selected_pcs = []

    if scenario in ("A", "B"):
        pcs = extract_pcs(full_desc)
        if not pcs:
            L("❌ ОШИБКА: ПК в описании не найдены!")
            _update_db_status(log_id, "Ошибка: нет ПК")
            return True
        L(f"💻 ПК из задачи: {', '.join(pcs)}")
        if not ensure_in_inventory(pcs, L):
            _update_db_status(log_id, "Ошибка инвентаря")
            return True
        selected_pcs = pcs

    elif scenario == "C":
        raw_count = getattr(issue.fields, ACCOUNTS_FIELD_ID, None)
        try:
            need = int(float(raw_count))
        except (TypeError, ValueError):
            L(f"❌ Некорректное количество ПК: {raw_count!r}")
            _update_db_status(log_id, "Ошибка парсинга")
            return True

        selected_pcs_dicts = pick_free_pcs(need, L)
        selected_pcs = [p["name"] for p in selected_pcs_dicts]
        if len(selected_pcs) < need:
            L(f"❌ Найдено {len(selected_pcs)} из {need}. Отмена.")
            _update_db_status(log_id, "Отмена: нехватка ПК", ",".join(selected_pcs) or "N/A")
            return True

        run_ssh_init_if_needed(selected_pcs, L)
        if not ensure_in_inventory(selected_pcs, L):
            _update_db_status(log_id, "Ошибка инвентаря", ",".join(selected_pcs))
            return True

    target_pcs = ",".join(selected_pcs)
    pc_count = len(selected_pcs)
    ansible_cmd = build_ansible_cmd(scenario, target_pcs, pc_count)

    _update_db_status(log_id, "В процессе (Ansible)", target_pcs)
    L(f"🚀 Запускаем главный плейбук...")

    rc, out, status = run_ansible_blocking(ansible_cmd, log_id=log_id)

    mm_text = ""

    # Если всё прошло ИДЕАЛЬНО (код 0)
    if rc == 0:
        L(f"\n✅ Деплой завершен без ошибок! Закрываем задачу в Jira...")
        
        # Определяем текст комментария
        if scenario == "A":
            comment_text = "Комп(ы) передеплоены. Готовы к игре."
        elif scenario == "B":
            comment_text = "Комп передеплоен. Доступен в гр."
        elif scenario == "C":
            comment_text = f"Подобраны комп(ы) {target_pcs}. Задеплоены и готовы к игре."
        else:
            comment_text = "Задача успешно выполнена."

        try:
            # 1. Назначаем задачу на себя (на логин из конфига)
            try:
                jira.assign_issue(issue.key, JIRA_USER)
                L(f"👤 Задача назначена на {JIRA_USER}.")
            except Exception as e:
                L(f"⚠️ Не удалось назначить задачу на {JIRA_USER}: {e}")

            # 2. Оставляем коммент
            jira.add_comment(issue.key, comment_text)
            L(f"💬 Оставлен комментарий: {comment_text}")
            
            # 3. Ищем нужный транзишн (кнопку перевода статуса)
            transitions = jira.transitions(issue)
            target_id = None
            for t in transitions:
                if t['name'].lower() in ('готово', 'done', 'решена', 'закрыто', 'closed'):
                    target_id = t['id']
                    break
            
            # 4. Переводим статус
            if target_id:
                jira.transition_issue(issue, target_id)
                L(f"🔒 Задача успешно переведена в статус 'Готово'.")
            else:
                L(f"⚠️ Не удалось найти статус 'Готово'. Доступные варианты: {', '.join(t['name'] for t in transitions)}")
        except Exception as e:
            L(f"❌ Ошибка при закрытии задачи в Jira: {e}")

        # Формируем сообщение для MM о том, что всё закрыто
        mm_text = (
            f"@silent ✅ **Задача закрыта:** **{issue.key}**\n"
            f"💻 **ПК:** {target_pcs}\n"
            f"🛠 **Тип:** {task_type}\n"
            f"👤 **Исполнитель:** {JIRA_USER}\n"
            f"💬 **Комментарий:** {comment_text}"
        )

    # Если была ОШИБКА Ansible
    else:
        if scenario == "C":
            L("\n⚠️ Ошибка деплоя. Возвращаем статус ПК в Insight на 'Ожидает'...")
            for pc_data in selected_pcs_dicts:
                if insight_revert_pc(pc_data["id"]):
                    L(f"   └ Статус {pc_data['name']} успешно откачен.")
                else:
                    L(f"   └ Ошибка отката статуса для {pc_data['name']}.")
                    
        # Формируем тревожное сообщение для MM
        mm_text = (
            f"@here ❌ **Ошибка деплоя:** **{issue.key}**\n"
            f"💻 **ПК:** {target_pcs}\n"
            f"🛠 **Тип:** {task_type}\n"
            f"**Статус Ansible:** {status}"
        )

    L(f"\n🏁 Финальный статус: {status}")
    _update_db_status(log_id, status)

    # Отправляем сообщение в MM только в самом конце
    send_mattermost(mm_text)
    
    return True


def bot_job():
    log.info("Jira worker started via APScheduler")
    try:
        jira = get_jira()
    except Exception:
        log.exception("Cannot connect to Jira")
        return

    jql = 'project = ACC AND assignee is EMPTY AND status = "To Do"'
    try:
        issues = jira.search_issues(jql, maxResults=50)
    except Exception:
        log.exception("JQL failed")
        return

    processed_any = False
    for issue in issues:
        try:
            if process_issue(jira, issue):
                processed_any = True
        except Exception:
            log.exception("Unhandled error on %s", issue.key)
            db = SessionLocal()
            db.add(DeployLog(jira_key=issue.key, task_type="Ошибка бота", target_pcs="N/A", status="Критическая ошибка"))
            db.commit()
            db.close()

    if not processed_any:
        log.info("Подходящих задач на данный момент нету.")

if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(console_handler)
    print("=== ЗАПУСК БОТА В РУЧНОМ РЕЖИМЕ ===")
    bot_job()
