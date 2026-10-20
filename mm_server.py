import asyncio
import os
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

import config
from ansible_runner import cancel_deploy, recover_active_deploys, run_and_wait_detached
from bot import bot_job
from config import (
    ANSIBLE_TIMEOUT, BASE_DIR, DASH_TIMEZONE, INVENTORY_PATH, JIRA_SERVER, LOG_RETENTION_DAYS,
    SKIP_TAGS_DEFAULT, TARGET_GROUP, WATCHDOG_GRACE_SECONDS,
)
from database import DeployLog, cleanup_old_logs, get_active_log, init_db, session_scope, utcnow
from deploy_service import DeployBusy, ensure_in_inventory, start_manual_deploy
from inventory_manager import parse_inventory
from logging_setup import setup_logging
from notify import send_mattermost
from security import check_auth, csrf_protect, make_csrf_token
from settings_store import get_deploy_settings, is_bot_enabled, save_deploy_settings, set_bot_enabled
from statuses import is_active
from validation import DEPLOY_TYPES, ValidationError, parse_pc_list, validate_tags

log = setup_logging()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_alerted_stuck: set[int] = set()


def _fmt_duration(entry: DeployLog) -> str:
    if entry.updated_at and entry.created_at:
        dur = (entry.updated_at - entry.created_at).total_seconds()
        return f"{int(dur // 60)}м {int(dur % 60)}с"
    return "—"


def watchdog_job() -> None:
    """Алерт в Mattermost, если деплой висит дольше таймаута Ansible + запас."""
    limit = ANSIBLE_TIMEOUT + WATCHDOG_GRACE_SECONDS
    with session_scope() as db:
        for entry in db.query(DeployLog).all():
            if not is_active(entry.status) or entry.id in _alerted_stuck or not entry.created_at:
                continue
            if (utcnow() - entry.created_at).total_seconds() > limit:
                _alerted_stuck.add(entry.id)
                send_mattermost(
                    f"@here ⏱ **Деплой #{entry.id} завис** ({entry.jira_key}, ПК: {entry.target_pcs}). "
                    f"Статус «{entry.status}» дольше {limit // 60} мин."
                )


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.validate_config()
    config.ensure_dirs()
    init_db()
    recover_active_deploys()

    scheduler = BackgroundScheduler()
    scheduler.add_job(bot_job, 'interval', minutes=2, max_instances=1, coalesce=True)
    scheduler.add_job(lambda: cleanup_old_logs(LOG_RETENTION_DAYS), 'interval', days=1)
    scheduler.add_job(watchdog_job, 'interval', minutes=5, max_instances=1)
    scheduler.start()

    try:
        cleanup_old_logs(LOG_RETENTION_DAYS)
    except Exception:
        log.exception("Retention failed at startup")

    yield
    scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError):
    return PlainTextResponse(str(exc), status_code=400)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics(user: str = Depends(check_auth)):
    """Метрики в формате Prometheus."""
    with session_scope() as db:
        rows = db.query(DeployLog.status).all()
    active = sum(1 for (s,) in rows if is_active(s))
    ok = sum(1 for (s,) in rows if (s or "").startswith("Успешно"))
    lines = [
        "# TYPE deploy_total counter", f"deploy_total {len(rows)}",
        "# TYPE deploy_success_total counter", f"deploy_success_total {ok}",
        "# TYPE deploy_active gauge", f"deploy_active {active}",
        "# TYPE deploy_bot_enabled gauge", f"deploy_bot_enabled {int(is_bot_enabled())}",
    ]
    return "\n".join(lines) + "\n"


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, limit: int = 300, user: str = Depends(check_auth)):
    limit = max(1, min(limit, 1000))
    tz = ZoneInfo(DASH_TIMEZONE)
    with session_scope() as db:
        logs = db.query(DeployLog).order_by(DeployLog.id.desc()).limit(limit).all()
        for entry in logs:
            entry.display_time = (
                entry.created_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).strftime('%Y-%m-%d %H:%M:%S')
                if entry.created_at else "—"
            )
            entry.duration = _fmt_duration(entry)
        db.expunge_all()

    return templates.TemplateResponse(
        request=request, name="index.html",
        context={
            "logs": logs,
            "jira_url": JIRA_SERVER,
            "skip_tags_default": SKIP_TAGS_DEFAULT,
            "csrf_token": make_csrf_token(user),
        },
    )


@app.get("/inventory", response_class=HTMLResponse)
def view_inventory(request: Request, user: str = Depends(check_auth)):
    groups = parse_inventory(INVENTORY_PATH)
    raw = INVENTORY_PATH.read_text(encoding="utf-8") if INVENTORY_PATH.exists() else "Файл не найден."

    clubgg_pcs: list[str] = []
    for g, hosts in groups.items():
        if g.lower() == TARGET_GROUP.lower():
            clubgg_pcs = hosts
            break

    return templates.TemplateResponse(
        request=request, name="inventory.html",
        context={
            "raw_content": raw,
            "clubgg_pcs": clubgg_pcs,
            "target_group": TARGET_GROUP,
            "csrf_token": make_csrf_token(user),
        },
    )


@app.post("/inventory/add")
def manual_inventory_add(new_pcs: str = Form(...), user: str = Depends(csrf_protect)):
    pcs = parse_pc_list(new_pcs)
    if not pcs:
        raise HTTPException(status_code=400, detail="Не указаны имена ПК")

    with session_scope() as db:
        if get_active_log(db):
            raise HTTPException(
                status_code=400,
                detail="Инвентарь занят: выполняется деплой. Подождите завершения задачи.",
            )

    ok, _ = ensure_in_inventory(pcs)
    if not ok:
        raise HTTPException(status_code=500, detail="Ошибка при записи в файл инвентаря.")
    log.info("Inventory add by %s: %s", user, ",".join(pcs))
    return RedirectResponse("/inventory", status_code=303)


@app.get("/api/statuses")
def api_statuses(user: str = Depends(check_auth)):
    with session_scope() as db:
        logs = db.query(DeployLog).order_by(DeployLog.id.desc()).limit(100).all()
        return {
            entry.id: {"status": entry.status or "Неизвестно", "duration": _fmt_duration(entry)}
            for entry in logs
        }


@app.get("/api/settings")
def api_get_settings(user: str = Depends(check_auth)):
    settings = get_deploy_settings()
    return {
        "bot_enabled": is_bot_enabled(),
        "full_args": settings["full_args"],
        "room_args": settings["room_args"],
    }


@app.post("/api/settings")
async def api_save_settings(request: Request, user: str = Depends(csrf_protect)):
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Ожидается JSON-объект")
    # Аргументы Ansible проверяются по белому списку (--tags/--skip-tags/-f), см. validation.py
    current = get_deploy_settings()
    save_deploy_settings(
        str(data.get("full_args") or current["full_args"]),
        str(data.get("room_args") or current["room_args"]),
    )
    set_bot_enabled(bool(data.get("bot_enabled", False)))
    log.info("Settings changed by %s (bot_enabled=%s)", user, bool(data.get("bot_enabled", False)))
    return {"status": "ok"}


def _launch(background_tasks: BackgroundTasks, *, deploy_type: str, pcs: list[str],
            skip_tags: str, jira_key: str, suffix: str, user: str) -> None:
    try:
        log_id, cmd = start_manual_deploy(
            deploy_type=deploy_type, pcs=pcs, skip_tags=skip_tags,
            jira_key=jira_key, task_suffix=suffix, user=user,
        )
    except DeployBusy as e:
        raise HTTPException(status_code=400, detail=f"Инвентарь занят: {e}")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    log.info("Deploy #%s started by %s: %s", log_id, user, " ".join(cmd))
    background_tasks.add_task(run_and_wait_detached, cmd, log_id)


@app.post("/manual_deploy")
def manual_deploy(
    background_tasks: BackgroundTasks,
    target_pcs: str = Form(...),
    deploy_type: str = Form(...),
    skip_tags: str = Form(""),
    user: str = Depends(csrf_protect),
):
    if deploy_type not in DEPLOY_TYPES:
        raise HTTPException(status_code=400, detail="Неизвестный тип деплоя")
    pcs = parse_pc_list(target_pcs)
    if not pcs:
        return RedirectResponse("/", status_code=303)
    skip_tags = validate_tags(skip_tags)

    _launch(background_tasks, deploy_type=deploy_type, pcs=pcs, skip_tags=skip_tags,
            jira_key="Ручной запуск", suffix="", user=user)
    return RedirectResponse("/", status_code=303)


@app.post("/repeat/{log_id}")
def repeat_deploy(log_id: int, background_tasks: BackgroundTasks, user: str = Depends(csrf_protect)):
    with session_scope() as db:
        old = db.query(DeployLog).filter(DeployLog.id == log_id).first()
        if not old:
            raise HTTPException(404)
        pcs = parse_pc_list((old.target_pcs or "").replace(",", " "))
        dtype = old.deploy_type if old.deploy_type in DEPLOY_TYPES else "room_only"
        stags = old.skip_tags or ""

    if not pcs:
        return RedirectResponse("/", status_code=303)

    _launch(background_tasks, deploy_type=dtype, pcs=pcs, skip_tags=stags,
            jira_key=f"Повтор #{log_id}", suffix=" (повтор)", user=user)
    return RedirectResponse("/", status_code=303)


@app.post("/cancel/{log_id}")
def cancel(log_id: int, user: str = Depends(csrf_protect)):
    ok = cancel_deploy(log_id)
    log.info("Cancel #%s by %s: %s", log_id, user, ok)
    return {"cancelled": ok}


def _status_of(log_id: int) -> str:
    with session_scope() as db:
        entry = db.query(DeployLog).filter(DeployLog.id == log_id).first()
        return entry.status if entry else ""


@app.get("/logs/{log_id}/stream")
async def stream_logs(log_id: int, user: str = Depends(check_auth)):
    with session_scope() as db:
        dblog = db.query(DeployLog).filter(DeployLog.id == log_id).first()
        if not dblog:
            raise HTTPException(404)
        log_file = dblog.log_file

    async def gen():
        if not log_file or not os.path.exists(log_file):
            yield "data: (лог-файл недоступен или уже удалён)\n\n"
            yield "data: [END]\n\n"
            return
        idle = 0
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            while True:
                line = f.readline()
                if line:
                    idle = 0
                    yield f"data: {line.rstrip()}\n\n"
                    continue
                # Статус в БД проверяем не на каждой пустой итерации и вне event loop
                idle += 1
                if idle % 3 == 1:
                    status = await asyncio.to_thread(_status_of, log_id)
                    if not is_active(status):
                        # дочитать то, что дописали между чтением и проверкой статуса
                        rest = f.read()
                        for ln in rest.splitlines():
                            yield f"data: {ln}\n\n"
                        yield "data: [END]\n\n"
                        return
                await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream")
