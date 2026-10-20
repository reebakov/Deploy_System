import re

from ansible_runner import _log_path, run_ansible_blocking, summarize_errors
from config import (
    ACCOUNTS_FIELD_ID, EPIC_FIELD_ID, INSIGHT_GPU_MODELS, JIRA_DONE_TRANSITIONS, JIRA_JQL,
    JIRA_MAX_RESULTS, JIRA_USER, POKER_FIELD_ID, PROBLEM_DESC_FIELD_ID, ROLE_FIELD_ID,
    STATE_FIELD_ID, TARGET_GROUP, TASK_EPICS, TASK_POKER, TASK_ROLE, BOT_RETRY_COOLDOWN_MIN,
    PLAYBOOK_OPENSSH,
)
from database import DeployLog, session_scope, utcnow, get_active_log
from datetime import timedelta
from deploy_service import (
    DeployBusy, build_command, create_deploy_log, ensure_in_inventory, update_log,
)
from inventory_manager import remove_pcs_from_inventory
from jira_clients import (
    get_jira, iql_quote, insight_search, insight_update_pc, safe_revert_pc,
)
from logging_setup import setup_logging
from notify import send_mattermost
from pc_checks import check_ping_many, check_ssh_many
from settings_store import is_bot_enabled
from statuses import Status, is_partial
from validation import PC_NAME_RE, sanitize_pc_name  # noqa: F401  (sanitize_pc_name реэкспортируется)

log = setup_logging()

IQL_FREE_PC = (
    'objectType = "Automat" AND Status = "Ожидает" AND '
    'Type IN ("Host Emulator", "Physical") AND "Видеокарта" IN ('
    + ", ".join(f'"{iql_quote(g)}"' for g in INSIGHT_GPU_MODELS)
    + ')'
)

# Имя ПК как самостоятельный токен: не «A105-B» и не часть идентификатора
PC_PATTERN = re.compile(r'(?<![\w-])[AaАа]\d{2,6}(?![\w-])')


# --- Классификация задачи ---------------------------------------------------

def classify_issue(issue):
    """Возвращает (сценарий A|B|C, название, полный текст) или (None, None, текст)."""
    f = issue.fields
    desc = (f.description or "").lower()
    epic_val = str(getattr(f, EPIC_FIELD_ID, '') or '').upper()
    poker_val = str(getattr(f, POKER_FIELD_ID, '') or '').upper()
    role_val = str(getattr(f, ROLE_FIELD_ID, '') or '').upper()
    state_val = str(getattr(f, STATE_FIELD_ID, '') or '').lower()
    problem_desc = str(getattr(f, PROBLEM_DESC_FIELD_ID, '') or '').lower()
    full_desc = f"{desc} {problem_desc}"

    if not (TASK_POKER in poker_val and TASK_ROLE in role_val):
        return None, None, full_desc
    if not any(epic in epic_val for epic in TASK_EPICS):
        return None, None, full_desc

    if "комп есть, требуется подготовка без интернета" in state_val:
        return "A", "Деплой ПК (уже есть)", full_desc
    if "нет в гр" in full_desc or "не отображается в гр" in full_desc:
        return "B", "Доработка (нет в ГР)", full_desc
    if "комп новый, требуется подбор" in state_val:
        return "C", "Деплой новых ПК", full_desc
    return None, None, full_desc


def extract_pcs(text: str) -> list[str]:
    return sorted({sanitize_pc_name(m) for m in PC_PATTERN.findall(text)})


# --- Шаги сценариев ----------------------------------------------------------

def pick_free_pcs(need: int, L) -> list[dict]:
    """Подбирает свободные пингуемые ПК в Insight и бронирует их."""
    L(f"🔍 Требуется подобрать ПК: {need} шт. Идем в Insight...")
    try:
        objects = insight_search(IQL_FREE_PC, result_per_page=500)
    except Exception as e:
        L(f"❌ Ошибка получения данных из Insight: {e}")
        return []

    # Имя из Insight попадает в команду ping/ansible — берём только валидные
    candidates = [o for o in objects if PC_NAME_RE.match(sanitize_pc_name(o.get("label") or ""))]
    ping_results = check_ping_many([c["label"] for c in candidates])

    selected: list[dict] = []
    for obj in candidates:
        if len(selected) >= need:
            break
        name = obj["label"]
        if not ping_results.get(name):
            L(f"❌ ПК {name} не пингуется, ищем дальше.")
            continue
        L(f"✅ ПК {name} доступен. Бронируем...")
        try:
            booked = insight_update_pc(obj["id"])
        except Exception as e:
            L(f"⚠️ Ошибка бронирования {name}: {e}")
            continue
        if booked:
            selected.append({"name": sanitize_pc_name(name), "id": obj["id"]})
            L(f"   └ Статус {name} обновлён в Insight")
        else:
            L(f"⚠️ Не удалось обновить статус {name} в Insight.")
    return selected


def revert_bookings(pcs: list[dict], L) -> None:
    for pc in pcs:
        if safe_revert_pc(pc["id"]):
            L(f"   └ Статус {pc['name']} успешно откачен.")
        else:
            L(f"   └ ⚠️ Ошибка отката статуса для {pc['name']} — исправьте в Insight вручную!")


def run_ssh_init_if_needed(pcs: list[str], L) -> None:
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


def fill_pcs_in_description(old_desc: str, pcs: list[str]) -> tuple[str, int]:
    """Вписывает имена ПК в пустые первые ячейки wiki-таблицы. Возвращает (текст, вписано)."""
    new_lines: list[str] = []
    pc_idx = 0
    for line in (old_desc or "").split('\n'):
        stripped = line.strip()
        if stripped.startswith('|') and not stripped.startswith('||'):
            cells = line.split('|')
            if len(cells) > 1 and pc_idx < len(pcs):
                first_cell = cells[1].strip()
                if not first_cell or first_cell == '&nbsp;':
                    cells[1] = f" {pcs[pc_idx]} "
                    line = '|'.join(cells)
                    pc_idx += 1
        new_lines.append(line)
    if pc_idx < len(pcs):
        new_lines.append("\n*Дополнительные ПК:* " + ", ".join(pcs[pc_idx:]))
    return '\n'.join(new_lines), pc_idx


def close_issue(jira, issue, comment_text: str, L) -> None:
    try:
        jira.add_comment(issue.key, comment_text)
        L(f"💬 Оставлен комментарий: {comment_text}")
        transitions = jira.transitions(issue)
        target_id = next((t['id'] for t in transitions if t['name'].lower() in JIRA_DONE_TRANSITIONS), None)
        if target_id:
            jira.transition_issue(issue, target_id)
            L("🔒 Задача успешно переведена в статус 'Готово'.")
        else:
            L(f"⚠️ Не удалось найти статус 'Готово'. Доступные варианты: "
              f"{', '.join(t['name'] for t in transitions)}")
    except Exception as e:
        L(f"❌ Ошибка при закрытии задачи в Jira: {e}")


def _fail(jira, issue, log_id: int, status: str, comment: str, mm: str | None = None) -> bool:
    update_log(log_id, status)
    try:
        jira.add_comment(issue.key, comment)
    except Exception:
        log.exception("Cannot comment %s", issue.key)
    if mm:
        send_mattermost(mm)
    return True


# --- Основной обработчик -----------------------------------------------------

def process_issue(jira, issue) -> bool:
    scenario, task_type, full_desc = classify_issue(issue)
    if not scenario:
        return False

    deploy_type = "full" if scenario == "C" else "room_only"
    try:
        log_id = create_deploy_log(
            jira_key=issue.key, task_type=task_type, target_pcs=Status.PREPARING,
            status=Status.INIT, deploy_type=deploy_type, triggered_by=f"bot:{JIRA_USER}",
        )
    except DeployBusy:
        log.info("Another deploy is currently running. Skipping %s.", issue.key)
        return True  # занято — выходим из тика, не перебирая остальные задачи

    log_file = str(_log_path(log_id))

    def L(msg: str):
        log.info("[%s] %s", issue.key, msg)
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except OSError:
            log.warning("Cannot write to %s", log_file)

    L(f"--- Задача [{issue.key}] {issue.fields.summary} — {task_type} ---")

    booked: list[dict] = []        # забронированные в Insight (для отката)
    added_to_inventory: list[str] = []   # добавленные в инвентарь этим запуском (для отката)
    try:
        return _run_scenario(jira, issue, scenario, task_type, full_desc, log_id, L, booked, added_to_inventory)
    except Exception as e:
        log.exception("Unhandled error on %s", issue.key)
        L(f"❌ Необработанная ошибка: {e}. Откатываем изменения...")
        revert_bookings(booked, L)
        remove_pcs_from_inventory(added_to_inventory)
        update_log(log_id, Status.CRITICAL)
        send_mattermost(f"@here ❌ **Критическая ошибка бота:** {issue.key}: {e}")
        return True


def _run_scenario(jira, issue, scenario, task_type, full_desc, log_id, L, booked, added) -> bool:
    try:
        jira.assign_issue(issue.key, JIRA_USER)
        L(f"👤 Задача взята в работу ({JIRA_USER}).")
    except Exception as e:
        L(f"⚠️ Не удалось назначить задачу: {e}. Продолжаем.")

    selected_pcs: list[str] = []

    if scenario in ("A", "B"):
        pcs = extract_pcs(full_desc)
        if not pcs:
            L("❌ ОШИБКА: ПК в описании не найдены!")
            return _fail(jira, issue, log_id, Status.NO_PCS,
                         "❌ Бот не смог найти имена ПК (формата A105) в описании задачи.",
                         f"⚠️ **Внимание:** Бот не нашел имена ПК в задаче {issue.key}")
        L(f"💻 ПК из задачи: {', '.join(pcs)}")
        ok, new_pcs = ensure_in_inventory(pcs)
        added.extend(new_pcs)
        if not ok:
            L("❌ Ошибка записи в файл инвентаря.")
            return _fail(jira, issue, log_id, Status.INV_ERROR,
                         "❌ Ошибка при добавлении ПК в файл инвентаря.",
                         f"⚠️ **Внимание:** Ошибка инвентаря для {issue.key}")
        if new_pcs:
            L(f"✅ ПК {', '.join(new_pcs)} добавлены в инвентарь {TARGET_GROUP}.")
        run_ssh_init_if_needed(pcs, L)  # недоступные по SSH ПК — главная причина падений (rc=4)
        selected_pcs = pcs

    else:  # C
        raw_count = getattr(issue.fields, ACCOUNTS_FIELD_ID, None)
        try:
            need = int(float(raw_count))
            if need < 1:
                raise ValueError
        except (TypeError, ValueError):
            L(f"❌ Некорректное количество ПК: {raw_count!r}")
            return _fail(jira, issue, log_id, Status.PARSE_ERROR,
                         f"❌ Некорректное количество ПК: {raw_count!r}")

        booked.extend(pick_free_pcs(need, L))
        selected_pcs = [p["name"] for p in booked]
        if len(selected_pcs) < need:
            L(f"❌ Найдено {len(selected_pcs)} из {need}. Отмена. Откатываем статусы...")
            revert_bookings(booked, L)
            booked.clear()
            update_log(log_id, Status.NOT_ENOUGH, ",".join(selected_pcs) or "N/A")
            return _fail(jira, issue, log_id, Status.NOT_ENOUGH,
                         f"❌ Найдено только {len(selected_pcs)} из {need} нужных ПК. Деплой отменен.",
                         f"⚠️ **Внимание:** Нехватка ПК ({len(selected_pcs)}/{need}) для {issue.key}")

        run_ssh_init_if_needed(selected_pcs, L)
        ok, new_pcs = ensure_in_inventory(selected_pcs)
        added.extend(new_pcs)
        if not ok:
            L("\n⚠️ Ошибка записи в инвентарь. Откатываем ПК...")
            revert_bookings(booked, L)
            booked.clear()
            update_log(log_id, Status.INV_ERROR, ",".join(selected_pcs))
            return True

    target_pcs = ",".join(selected_pcs)
    _, ansible_cmd = build_command("full" if scenario == "C" else "room_only", selected_pcs)

    update_log(log_id, Status.RUNNING, target_pcs, ansible_cmd)
    L("🚀 Запускаем главный плейбук...")
    rc, _out, status = run_ansible_blocking(ansible_cmd, log_id=log_id)

    if rc == 0:
        mm_text = _on_success(jira, issue, scenario, task_type, selected_pcs, target_pcs, L)
    else:
        errors = summarize_errors(_log_path(log_id))
        mm_text = _on_failure(jira, issue, scenario, task_type, selected_pcs, target_pcs,
                              status, booked, added, L, errors)
        update_log(log_id, errors=errors)

    L(f"\n🏁 Финальный статус: {status}")
    update_log(log_id, status)
    send_mattermost(mm_text)
    return True


def _on_success(jira, issue, scenario, task_type, selected_pcs, target_pcs, L) -> str:
    L("\n✅ Деплой завершен без ошибок! Закрываем задачу в Jira...")

    if scenario == "C" and selected_pcs:
        new_desc, filled = fill_pcs_in_description(issue.fields.description or "", selected_pcs)
        try:
            issue.update(description=new_desc)
            L(f"📝 Описание задачи обновлено. ПК вписаны в таблицу: {', '.join(selected_pcs[:filled])}")
        except Exception as e:
            L(f"⚠️ Не удалось обновить описание задачи: {e}")

    comment_text = {
        "A": "Комп(ы) передеплоены. Готовы к игре.",
        "B": "Комп передеплоен. Доступен в гр.",
        "C": f"Подобраны комп(ы) {target_pcs}. Задеплоены и готовы к игре.",
    }.get(scenario, "Задача успешно выполнена.")

    close_issue(jira, issue, comment_text, L)
    return (
        f"@silent ✅ **Задача закрыта:** **{issue.key}**\n"
        f"💻 **ПК:** {target_pcs}\n"
        f"🛠 **Тип:** {task_type}\n"
        f"👤 **Исполнитель:** `{JIRA_USER}`\n"
        f"💬 **Комментарий:** {comment_text}"
    )


def _on_failure(jira, issue, scenario, task_type, selected_pcs, target_pcs, status, booked, added, L,
                errors: str = "") -> str:
    extra = f"\n```\n{errors}\n```" if errors else ""
    if is_partial(status):
        L("\n⚠️ Частичный успех. ПК остаются в инвентаре и забронированными в Insight. Проверьте руками.")
        try:
            jira.add_comment(issue.key, f"⚠️ Деплой завершен частично ({status}). Проверьте логи.")
        except Exception:
            log.exception("Cannot comment %s", issue.key)
        if booked:
            extra += f"\n⚠️ **Требуется ручная проверка**: ПК {target_pcs} остались забронированными в Insight."
    else:
        if booked:
            L("\n⚠️ Ошибка деплоя. Возвращаем статус ПК в Insight на 'Ожидает'...")
            revert_bookings(booked, L)
        if added:
            L(f"↩️ Убираем из инвентаря добавленные ПК: {', '.join(added)}")
            remove_pcs_from_inventory(added)
        try:
            jira.add_comment(issue.key, "❌ Ошибка выполнения Ansible. ПК откреплены." if booked
                             else "❌ Ошибка выполнения Ansible. Проверьте логи.")
        except Exception:
            log.exception("Cannot comment %s", issue.key)

    return (
        f"@here ❌ **Ошибка деплоя:** **{issue.key}**\n"
        f"💻 **ПК:** {target_pcs}\n"
        f"🛠 **Тип:** {task_type}\n"
        f"**Статус Ansible:** {status}{extra}"
    )


# --- Планировщик -------------------------------------------------------------

def _recently_processed(key: str) -> bool:
    cutoff = utcnow() - timedelta(minutes=BOT_RETRY_COOLDOWN_MIN)
    with session_scope() as db:
        return db.query(DeployLog).filter(
            DeployLog.jira_key == key, DeployLog.created_at >= cutoff
        ).first() is not None


def bot_job():
    log.info("Jira worker started via APScheduler")

    if not is_bot_enabled():
        log.info("Авто-бот выключен тумблером. Спим...")
        return

    with session_scope() as db:
        if get_active_log(db):
            log.info("Another deploy is currently running. Skipping bot tick.")
            return

    try:
        jira = get_jira()
    except Exception:
        log.exception("Cannot connect to Jira")
        return

    try:
        issues = jira.search_issues(JIRA_JQL, maxResults=JIRA_MAX_RESULTS)
    except Exception:
        log.exception("JQL failed")
        return

    for issue in issues:
        if _recently_processed(issue.key):
            continue  # идемпотентность: не берём одну и ту же задачу в каждом тике
        try:
            if process_issue(jira, issue):
                return
        except Exception:
            log.exception("Unhandled error on %s", issue.key)

    log.info("Подходящих задач на данный момент нету.")


if __name__ == "__main__":
    from database import init_db
    from config import ensure_dirs
    ensure_dirs()
    init_db()
    print("=== ЗАПУСК БОТА В РУЧНОМ РЕЖИМЕ ===")
    bot_job()
