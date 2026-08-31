"""valetudo-restore: backup / restore / watchdog for Valetudo on Dreame robots."""
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, service, store
from .models import (BACKUP_DIR, CONFIG_DIR, Settings, load_settings,
                     save_settings)

logging.basicConfig(
    level=os.environ.get("VR_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
)
log = logging.getLogger("vr")

BASE = Path(__file__).parent
scheduler = BackgroundScheduler(timezone=os.environ.get("TZ", "UTC"))


def reschedule() -> None:
    """(Re)install jobs from current settings. Safe to call repeatedly."""
    s = load_settings()
    for job_id in ("backup", "monitor"):
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass
    if s.backup_enabled:
        scheduler.add_job(
            service.run_backup, CronTrigger(hour=s.backup_cron_hour,
                                            minute=s.backup_cron_minute),
            id="backup", kwargs={"kind": "scheduled"},
            max_instances=1, coalesce=True, misfire_grace_time=3600,
        )
    if s.monitor_enabled:
        scheduler.add_job(
            service.monitor_tick,
            IntervalTrigger(minutes=s.poll_interval_minutes),
            id="monitor", max_instances=1, coalesce=True,
            misfire_grace_time=300,
        )
    log.info("scheduler: backup=%s@%s monitor=%s/%dmin",
             s.backup_enabled, s.cron_summary(),
             s.monitor_enabled, s.poll_interval_minutes)


@asynccontextmanager
async def lifespan(app: FastAPI):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    store.init_db()
    # First run: persist whatever the environment seeded so the UI shows it and
    # later env changes cannot silently override a UI edit.
    from .models import SETTINGS_FILE, settings_from_env
    if not SETTINGS_FILE.exists():
        seeded = settings_from_env()
        save_settings(load_settings())
        if seeded:
            log.info("seeded initial settings from environment: %s",
                     ", ".join(sorted(seeded)))
            store.log_event("info", "settings",
                            "seeded from environment: %s" % ", ".join(sorted(seeded)))
    store.log_event("info", "app", "valetudo-restore %s started" % __version__)
    reschedule()
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="valetudo-restore", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


def _fmt_ts(ts: int) -> str:
    if not ts:
        return "never"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


templates.env.filters["ts"] = _fmt_ts
templates.env.filters["size"] = lambda n: (
    "%.1f MB" % (n / 1048576) if n and n >= 1048576
    else ("%.1f KB" % (n / 1024) if n else "0")
)


# ---------------------------------------------------------------- pages ----
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    s = load_settings()
    store.reconcile_backups(BACKUP_DIR)
    mon = store.kv_get("monitor", {})
    return templates.TemplateResponse("index.html", {
        "request": request,
        "s": s,
        "version": __version__,
        "monitor": mon,
        "backups": store.list_backups()[:25],
        "events": store.recent_events(40),
        "last_backup_ok": store.kv_get("last_backup_ok", 0),
        "last_restore": store.kv_get("last_restore", {}),
        "binary_cached": service.binary_cache_path(s.valetudo_arch).exists(),
        "key_present": Path(s.ssh_key_path).exists(),
    })


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {
        "request": request, "s": load_settings(), "version": __version__,
    })


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    cur = load_settings().model_dump()
    for k, v in form.items():
        if k not in cur:
            continue
        default = cur[k]
        if isinstance(default, bool):
            cur[k] = str(v).lower() in ("1", "true", "on", "yes")
        elif isinstance(default, int):
            try:
                cur[k] = int(v)
            except (TypeError, ValueError):
                pass
        else:
            cur[k] = v
    # unchecked checkboxes are simply absent from the form body
    for k, v in load_settings().model_dump().items():
        if isinstance(v, bool) and k not in form:
            cur[k] = False
    try:
        s = Settings(**cur)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    save_settings(s)
    store.log_event("info", "settings", "settings updated")
    reschedule()
    return RedirectResponse("/settings?saved=1", status_code=303)


# ----------------------------------------------------------------- api -----
@app.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__}


@app.get("/api/status")
def api_status():
    s = load_settings()
    return {
        "version": __version__,
        "monitor": store.kv_get("monitor", {}),
        "last_backup_ok": store.kv_get("last_backup_ok", 0),
        "last_restore": store.kv_get("last_restore", {}),
        "backups": len(store.list_backups()),
        "auto_restore": s.auto_restore,
        "key_present": Path(s.ssh_key_path).exists(),
    }


@app.post("/api/test-connection")
def api_test():
    s = load_settings()
    state, p = service.classify(s)
    return {
        "state": state,
        "ssh_ok": p.ssh_ok,
        "binary_present": p.binary_present,
        "config_present": p.config_present,
        "valetudo_running": p.valetudo_running,
        "guard_running": p.guard_running,
        "uptime_s": p.uptime_s,
        "factory_log": p.factory_log,
        "error": p.error,
    }


@app.post("/api/backup")
def api_backup():
    return service.run_backup(kind="manual")


@app.post("/api/restore")
async def api_restore(filename: str = Form(default=""),
                      file: UploadFile = File(default=None)):
    blob = None
    if file is not None and getattr(file, "filename", ""):
        blob = await file.read()
        if len(blob) > 200 * 1024 * 1024:
            return JSONResponse({"ok": False, "error": "file too large"},
                                status_code=413)
    return service.run_restore(filename or None, reason="manual", blob=blob)


@app.post("/api/monitor-tick")
def api_tick():
    return service.monitor_tick()


@app.post("/api/download-binary")
def api_binary():
    s = load_settings()
    try:
        p = service.ensure_binary(s, force=True)
        return {"ok": True, "file": p.name, "size": p.stat().st_size}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/upload-key")
async def api_upload_key(file: UploadFile = File(...)):
    """Store the SSH private key into the config volume with 0600."""
    s = load_settings()
    data = await file.read()
    if b"PRIVATE KEY" not in data:
        return JSONResponse(
            {"ok": False, "error": "that does not look like a private key"},
            status_code=400)
    dest = Path(s.ssh_key_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data.replace(b"\r\n", b"\n"))
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    store.log_event("info", "settings", "ssh key uploaded (%d bytes)" % len(data))
    return {"ok": True, "path": str(dest), "bytes": len(data)}


@app.post("/api/restart-valetudo")
def api_restart_valetudo():
    return service.restart_valetudo()


@app.post("/api/reboot-robot")
def api_reboot_robot():
    return service.reboot_robot()


@app.post("/api/test-webhook")
def api_test_webhook():
    return service.test_webhook()


@app.post("/api/restore-map")
async def api_restore_map(
    file: UploadFile = File(default=None),
    filename: str = Form(default=""),
    force: str = Form(default=""),
):
    """Restore /data/map from an uploaded archive, or from a stored backup."""
    blob = None
    if file is not None and getattr(file, "filename", ""):
        blob = await file.read()
        if len(blob) > 200 * 1024 * 1024:
            return JSONResponse({"ok": False, "error": "file too large"},
                                status_code=413)
    return service.restore_map(blob=blob, filename=filename or None,
                               force=str(force).lower() in ("1","true","on","yes"))


@app.get("/api/backups")
def api_backups():
    store.reconcile_backups(BACKUP_DIR)
    return store.list_backups()


@app.get("/api/backups/{filename}")
def api_download(filename: str):
    p = (BACKUP_DIR / filename).resolve()
    if p.parent != BACKUP_DIR.resolve() or not p.exists():
        raise HTTPException(404, "not found")
    return FileResponse(p, filename=filename, media_type="application/gzip")


@app.post("/api/backups/{filename}/delete")
def api_delete(filename: str):
    p = (BACKUP_DIR / filename).resolve()
    if p.parent != BACKUP_DIR.resolve():
        raise HTTPException(400, "bad path")
    if p.exists():
        p.unlink()
    store.forget_backup(filename)
    store.log_event("info", "backup", "deleted %s" % filename)
    return {"ok": True}


@app.get("/api/events")
def api_events(limit: int = 100):
    return store.recent_events(min(limit, 500))
