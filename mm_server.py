import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from datetime import timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from fastapi import (BackgroundTasks, Depends, FastAPI, Form, HTTPException,
                     Request)
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.background import BackgroundScheduler

from ansible_runner import cancel_deploy, run_and_wait_detached
from config import (
    DASH_PASS, DASH_USER, INVENTORY_PATH, JIRA_SERVER, LOG_RETENTION_DAYS,
    PLAYBOOK_MOBIDESK, PLAYBOOK_OPENSSH, SKIP_TAGS_DEFAULT, TARGET_GROUP,
    DEPLOY_LOG_DIR,
)
from database import DeployLog, SessionLocal, cleanup_old_logs
from inventory_manager import parse_inventory
from logging_setup import setup_logging
from bot import bot_job

log = setup_logging()
templates = Jinja2Templates(directory="templates")
security = HTTPBasic()


def check_auth(creds: HTTPBasicCredentials = Depends(security)) -> str:
    ok_user = secrets.compare_digest(creds.username, DASH_USER)
    ok_pass = secrets.compare_digest(creds.password, DASH_PASS)
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    return creds.username


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = BackgroundScheduler()
    # Пока авто-бот закомментирован для тестов:
    # scheduler.add_job(bot_job, 'interval', minutes=2, max_instances=1)
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
async def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(check_auth)):
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
async def view_inventory(request: Request, user: str = Depends(check_auth)):
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


@app.get("/api/statuses")
async def api_statuses(user: str = Depends(check_auth)):
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


def _build_manual_cmd(deploy_type: str, pcs: list[str], skip_tags: str):
    target = ",".join(pcs)
    if deploy_type == "full":
        task_name = "Полный деплой"
        cmd = ["ansible-playbook", PLAYBOOK_MOBIDESK, "-e", f"target={target}"]
        if skip_tags.strip():
            cmd += ["--skip-tags", skip_tags.strip()]
    elif deploy_type == "ssh":
        task_name = "Закинуть SSH"
        cmd = ["ansible-playbook", PLAYBOOK_OPENSSH, "-e", f"target={target}"]
    else:
        task_name = "Обновление рума"
        cmd = ["ansible-playbook", PLAYBOOK_MOBIDESK, "-e", f"target={target}",
               "--tags", "neoagent,clubgg"]
    if len(pcs) > 1 and deploy_type != "ssh":
        cmd += ["-f", str(len(pcs))]
    return task_name, cmd


@app.post("/manual_deploy")
async def manual_deploy(
    background_tasks: BackgroundTasks,
    target_pcs: str = Form(...),
    deploy_type: str = Form(...),
    skip_tags: str = Form(""),
    user: str = Depends(check_auth),
):
    pcs = [pc.strip().upper() for pc in target_pcs.split(",") if pc.strip()]
    if not pcs:
        return RedirectResponse("/", status_code=303)
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
    
    # Сразу ставим путь к файлу лога
    new_log.log_file = str(DEPLOY_LOG_DIR / f"deploy_{log_id}.log")
    db.commit()
    db.close()

    background_tasks.add_task(run_and_wait_detached, cmd, log_id)
    return RedirectResponse("/", status_code=303)


@app.post("/repeat/{log_id}")
async def repeat_deploy(log_id: int,
                        background_tasks: BackgroundTasks,
                        user: str = Depends(check_auth)):
    db = SessionLocal()
    try:
        old = db.query(DeployLog).filter(DeployLog.id == log_id).first()
        if not old:
            raise HTTPException(404)
        pcs = [p.strip() for p in (old.target_pcs or "").split(",") if p.strip()]
        dtype = old.deploy_type or "room_only"
        stags = old.skip_tags or ""
    finally:
        db.close()

    if not pcs:
        return RedirectResponse("/", status_code=303)
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
async def cancel(log_id: int, user: str = Depends(check_auth)):
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
                    if "В процессе" not in (cur_status or ""):
                        yield "data: [END]\n\n"
                        break
                    await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream")
