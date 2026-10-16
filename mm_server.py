import asyncio
import os
import secrets
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import (BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request)
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.background import BackgroundScheduler

from ansible_runner import cancel_deploy, run_and_wait_detached
from config import (
    BASE_DIR, DASH_PASS, DASH_USER, INVENTORY_PATH, JIRA_SERVER, LOG_RETENTION_DAYS,
    PLAYBOOK_MOBIDESK, PLAYBOOK_OPENSSH, SKIP_TAGS_DEFAULT, TARGET_GROUP,
    DEPLOY_LOG_DIR,
)
from database import DeployLog, SessionLocal, cleanup_old_logs
from inventory_manager import parse_inventory, check_pcs_in_inventory, add_pcs_to_inventory
from logging_setup import setup_logging
from bot import bot_job, sanitize_pc_name

log = setup_logging()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
security = HTTPBasic()

def check_auth(creds: HTTPBasicCredentials = Depends(security)) -> str:
    ok_user = secrets.compare_digest(creds.username, DASH_USER)
    ok_pass = secrets.compare_digest(creds.password, DASH_PASS)
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    return creds.username

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Очистка зависших статусов после рестарта сервиса
    db = SessionLocal()
    try:
        stuck_logs = db.query(DeployLog).filter(DeployLog.status.like("%В процессе%")).all()
        for sl in stuck_logs:
            sl.status = "Прервано (рестарт)"
        db.commit()
    finally:
        db.close()

    scheduler = BackgroundScheduler()
    # Авто-бот (расписание на каждые 2 минуты)
    scheduler.add_job(bot_job, 'interval', minutes=2, max_instances=1)
    scheduler.add_job(lambda: cleanup_old_logs(LOG_RETENTION_DAYS), 'interval', days=1)
    scheduler.start()
    
    try:
        cleanup_old_logs(LOG_RETENTION_DAYS)
    except Exception:
        log.exception("Retention failed at startup")
        
    yield
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: str = Depends(check_auth)):
    db = SessionLocal()
    try:
        logs = db.query(DeployLog).order_by(DeployLog.id.desc()).limit(1000).all()
        nsk = ZoneInfo("Asia/Novosibirsk")
        for l in logs:
            if l.created_at:
                dt = l.created_at.replace(tzinfo=timezone.utc)
                l.display_time = dt.astimezone(nsk).strftime('%Y-%m-%d %H:%M:%S')
            else:
                l.display_time = "—"
            if l.updated_at and l.created_at:
                dur = (l.updated_at - l.created_at).total_seconds()
                l.duration = f"{int(dur // 60)}м {int(dur % 60)}с"
            else:
                l.duration = "—"
    finally:
        db.close()

    return templates.TemplateResponse(
        request=request, name="index.html",
        context={
            "logs": logs,
            "jira_url": JIRA_SERVER,
            "skip_tags_default": SKIP_TAGS_DEFAULT,
        },
    )

@app.get("/inventory", response_class=HTMLResponse)
def view_inventory(request: Request, user: str = Depends(check_auth)):
    groups = parse_inventory(INVENTORY_PATH)
    raw = INVENTORY_PATH.read_text() if INVENTORY_PATH.exists() else "Файл не найден."

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
        },
    )

@app.post("/inventory/add")
def manual_inventory_add(
    new_pcs: str = Form(...),
    user: str = Depends(check_auth)
):
    db = SessionLocal()
    try:
        active_deploy = db.query(DeployLog).filter(DeployLog.status.like("%В процессе%")).first()
        if active_deploy:
            raise HTTPException(
                status_code=400, 
                detail="Инвентарь занят: выполняется деплой. Подождите завершения задачи."
            )
    finally:
        db.close()

    pcs = [sanitize_pc_name(pc) for pc in new_pcs.replace(" ", ",").split(",") if pc.strip()]
    pcs = [pc for pc in pcs if pc]
    
    if not pcs:
        raise HTTPException(status_code=400, detail="Не указаны имена ПК")

    found, missing = check_pcs_in_inventory(pcs)
    if missing:
        success = add_pcs_to_inventory(missing)
        if not success:
            raise HTTPException(status_code=500, detail="Ошибка при записи в файл инвентаря.")
            
    return RedirectResponse("/inventory", status_code=303)

@app.get("/api/statuses")
def api_statuses(user: str = Depends(check_auth)):
    db = SessionLocal()
    try:
        logs = db.query(DeployLog).order_by(DeployLog.id.desc()).limit(100).all()
        result = {}
        for l in logs:
            if l.updated_at and l.created_at:
                dur = (l.updated_at - l.created_at).total_seconds()
                duration_str = f"{int(dur // 60)}м {int(dur % 60)}с"
            else:
                duration_str = "—"
            
            result[l.id] = {
                "status": l.status or "Неизвестно",
                "duration": duration_str
            }
        return result
    finally:
        db.close()

def get_deploy_settings():
    sfile = BASE_DIR / "deploy_settings.json"
    if sfile.exists():
        try:
            return json.loads(sfile.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "full_args": f"--skip-tags {SKIP_TAGS_DEFAULT}",
        "room_args": "--tags neoagent,clubgg,neoagent_account_shortcuts"
    }

@app.get("/api/settings")
def api_get_settings(user: str = Depends(check_auth)):
    state_file = BASE_DIR / "bot_status.txt"
    is_enabled = False if not state_file.exists() or state_file.read_text().strip() == "0" else True
    settings = get_deploy_settings()
    return {
        "bot_enabled": is_enabled,
        "full_args": settings.get("full_args", f"--skip-tags {SKIP_TAGS_DEFAULT}"),
        "room_args": settings.get("room_args", "--tags neoagent,clubgg,neoagent_account_shortcuts")
    }

@app.post("/api/settings")
async def api_save_settings(request: Request, user: str = Depends(check_auth)):
    data = await request.json()
    
    bot_enabled = data.get("bot_enabled", False)
    state_file = BASE_DIR / "bot_status.txt"
    state_file.write_text("1" if bot_enabled else "0", encoding="utf-8")
    
    sfile = BASE_DIR / "deploy_settings.json"
    sfile.write_text(json.dumps({
        "full_args": data.get("full_args", ""),
        "room_args": data.get("room_args", "")
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    
    return {"status": "ok"}

def _build_manual_cmd(deploy_type: str, pcs: list[str], skip_tags: str):
    target = ",".join(pcs)
    settings = get_deploy_settings()
    
    if deploy_type == "full":
        task_name = "Полный деплой"
        cmd = ["ansible-playbook", PLAYBOOK_MOBIDESK, "-e", f"target={target}"]
        if skip_tags.strip():
            cmd += ["--skip-tags", skip_tags.strip()]
        else:
            cmd += settings.get("full_args", f"--skip-tags {SKIP_TAGS_DEFAULT}").split()
            
    elif deploy_type == "ssh":
        task_name = "Закинуть SSH"
        cmd = ["ansible-playbook", PLAYBOOK_OPENSSH, "-e", f"target={target}"]
    else:
        task_name = "Обновление рума"
        cmd = ["ansible-playbook", PLAYBOOK_MOBIDESK, "-e", f"target={target}"]
        cmd += settings.get("room_args", "--tags neoagent,clubgg,neoagent_account_shortcuts").split()
        
    if len(pcs) > 1 and deploy_type != "ssh":
        cmd += ["-f", str(len(pcs))]
    return task_name, cmd

@app.post("/manual_deploy")
def manual_deploy(
    background_tasks: BackgroundTasks,
    target_pcs: str = Form(...),
    deploy_type: str = Form(...),
    skip_tags: str = Form(""),
    user: str = Depends(check_auth),
):
    pcs = [sanitize_pc_name(pc) for pc in target_pcs.split(",") if pc.strip()]
    pcs = [pc for pc in pcs if pc]
    if not pcs:
        return RedirectResponse("/", status_code=303)
        
    db = SessionLocal()
    try:
        active_deploy = db.query(DeployLog).filter(
            DeployLog.status.in_(["В процессе (Ansible)", "Инициализация"])
        ).first()
        if active_deploy:
            raise HTTPException(
                status_code=400, 
                detail="Инвентарь занят: в данный момент уже выполняется деплой. Дождитесь завершения."
            )
    finally:
        db.close()

    found, missing = check_pcs_in_inventory(pcs)
    if missing:
        if not add_pcs_to_inventory(missing):
            raise HTTPException(status_code=500, detail="Ошибка при записи недостающих ПК в файл инвентаря.")

    target_str = ",".join(pcs)
    task_name, cmd = _build_manual_cmd(deploy_type, pcs, skip_tags)

    db = SessionLocal()
    new_log = DeployLog(
        jira_key="Ручной запуск",
        task_type=task_name,
        target_pcs=target_str,
        status="В процессе (Ansible)",
        skip_tags=skip_tags if deploy_type == "full" else None,
        deploy_type=deploy_type,
    )
    db.add(new_log); db.commit(); db.refresh(new_log)
    log_id = new_log.id
    
    new_log.log_file = str(DEPLOY_LOG_DIR / f"deploy_{log_id}.log")
    db.commit()
    db.close()

    background_tasks.add_task(run_and_wait_detached, cmd, log_id)
    return RedirectResponse("/", status_code=303)

@app.post("/repeat/{log_id}")
def repeat_deploy(log_id: int,
                  background_tasks: BackgroundTasks,
                  user: str = Depends(check_auth)):
    db = SessionLocal()
    try:
        old = db.query(DeployLog).filter(DeployLog.id == log_id).first()
        if not old:
            raise HTTPException(404)
        pcs = [sanitize_pc_name(p) for p in (old.target_pcs or "").split(",") if p.strip()]
        pcs = [p for p in pcs if p]
        dtype = old.deploy_type or "room_only"
        stags = old.skip_tags or ""
        
        active_deploy = db.query(DeployLog).filter(
            DeployLog.status.in_(["В процессе (Ansible)", "Инициализация"])
        ).first()
        if active_deploy:
            raise HTTPException(
                status_code=400, 
                detail="Инвентарь занят: в данный момент уже выполняется деплой. Дождитесь завершения."
            )
    finally:
        db.close()

    if not pcs:
        return RedirectResponse("/", status_code=303)
        
    found, missing = check_pcs_in_inventory(pcs)
    if missing:
        if not add_pcs_to_inventory(missing):
            raise HTTPException(status_code=500, detail="Ошибка при записи недостающих ПК в файл инвентаря.")

    target_str = ",".join(pcs)
    task_name, cmd = _build_manual_cmd(dtype, pcs, stags)
    task_name += " (повтор)"

    db = SessionLocal()
    new_log = DeployLog(
        jira_key=f"Повтор #{log_id}",
        task_type=task_name,
        target_pcs=target_str,
        status="В процессе (Ansible)",
        skip_tags=stags if dtype == "full" else None,
        deploy_type=dtype,
    )
    db.add(new_log); db.commit(); db.refresh(new_log)
    new_id = new_log.id
    
    new_log.log_file = str(DEPLOY_LOG_DIR / f"deploy_{new_id}.log")
    db.commit()
    db.close()

    background_tasks.add_task(run_and_wait_detached, cmd, new_id)
    return RedirectResponse("/", status_code=303)

@app.post("/cancel/{log_id}")
def cancel(log_id: int, user: str = Depends(check_auth)):
    ok = cancel_deploy(log_id)
    return {"cancelled": ok}

@app.get("/logs/{log_id}/stream")
async def stream_logs(log_id: int, user: str = Depends(check_auth)):
    db = SessionLocal()
    try:
        dblog = db.query(DeployLog).filter(DeployLog.id == log_id).first()
        if not dblog:
            raise HTTPException(404)
        log_file = dblog.log_file
    finally:
        db.close()

    async def gen():
        if not log_file or not os.path.exists(log_file):
            yield "data: (лог-файл недоступен или уже удалён)\n\n"
            yield "data: [END]\n\n"
            return
        with open(log_file, "r", errors="replace") as f:
            while True:
                line = f.readline()
                if line:
                    yield f"data: {line.rstrip()}\n\n"
                else:
                    db2 = SessionLocal()
                    try:
                        s = db2.query(DeployLog).filter(DeployLog.id == log_id).first()
                        cur_status = s.status if s else ""
                    finally:
                        db2.close()
                    if "В процессе" not in (cur_status or "") and "Инициализация" not in (cur_status or ""):
                        yield "data: [END]\n\n"
                        break
                    await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream")