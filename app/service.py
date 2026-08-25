"""
Backup, restore and monitoring logic.

Monitor states
--------------
HEALTHY   ssh ok, Valetudo binary present and serving
CRASHED   ssh ok, binary present, Valetudo not running  -> restart it
WIPED     ssh ok, binary MISSING                        -> full restore
NO_SSH    could not talk to the robot at all            -> never act on this
OFFLINE   robot not reachable on the network            -> not an alert

NO_SSH exists specifically so that a failure to observe can never be mistaken
for a wipe. Acting on an unverified state is how an automated recovery ends up
overwriting a perfectly healthy robot.
"""
from __future__ import annotations

import io
import json
import logging
import os
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from . import robot as R
from .models import BACKUP_DIR, CONFIG_DIR, Settings, load_settings
from . import store

log = logging.getLogger("vr.service")

VALETUDO_RELEASE = (
    "https://github.com/Hypfer/Valetudo/releases/latest/download/valetudo-{arch}"
)

STATE_HEALTHY = "HEALTHY"
STATE_CRASHED = "CRASHED"
STATE_WIPED = "WIPED"
STATE_NO_SSH = "NO_SSH"
STATE_OFFLINE = "OFFLINE"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _client(s: Settings) -> R.RobotClient:
    return R.RobotClient(s.robot_host, s.robot_port, s.robot_user,
                         s.ssh_key_path, s.ssh_timeout)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def binary_cache_path(arch: str) -> Path:
    return CONFIG_DIR / ("valetudo-" + arch)


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------
def notify(s: Settings, event: str, message: str, detail: Optional[dict] = None) -> None:
    if not s.webhook_url:
        return
    headers = {"Content-Type": "application/json"}
    if s.webhook_headers.strip():
        try:
            headers.update(json.loads(s.webhook_headers))
        except Exception as e:
            log.warning("bad webhook_headers JSON: %s", e)
    payload = {
        "source": "valetudo-restore",
        "event": event,
        "message": message,
        "robot": s.robot_host,
        "ts": int(time.time()),
        "detail": detail or {},
    }
    try:
        httpx.post(s.webhook_url, json=payload, headers=headers, timeout=10.0)
    except Exception as e:
        log.warning("webhook failed: %s", e)
        store.log_event("warn", "notify", "webhook failed: %s" % e)


# --------------------------------------------------------------------------
# valetudo binary
# --------------------------------------------------------------------------
def ensure_binary(s: Settings, force: bool = False) -> Path:
    """Download the Valetudo release binary into the config volume."""
    dest = binary_cache_path(s.valetudo_arch)
    if dest.exists() and not force:
        return dest
    url = VALETUDO_RELEASE.format(arch=s.valetudo_arch)
    store.log_event("info", "binary", "downloading %s" % url)
    tmp = dest.with_suffix(".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=120.0) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_bytes(65536):
                fh.write(chunk)
    tmp.replace(dest)
    store.log_event("info", "binary",
                    "cached %s (%d bytes)" % (dest.name, dest.stat().st_size))
    return dest


# --------------------------------------------------------------------------
# backup
# --------------------------------------------------------------------------
def run_backup(kind: str = "scheduled") -> dict:
    """
    Pull the robot's irreplaceable state into a single .tar.gz.

    The Valetudo binary is deliberately NOT included: it is a 37 MB file that
    is always re-downloadable from GitHub, whereas the per-robot identity and
    calibration data under /mnt/private cannot be regenerated at all.
    """
    s = load_settings()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    name = "valetudo-backup-%s.tar.gz" % _stamp()
    path = BACKUP_DIR / name
    manifest = {
        "created": datetime.now(timezone.utc).isoformat(),
        "robot": s.robot_host,
        "kind": kind,
        "items": [],
    }
    try:
        with _client(s) as c:
            probe = c.probe()
            if not probe.ssh_ok:
                raise R.RobotUnreachable(probe.error or "probe failed")
            manifest["uptime_s"] = probe.uptime_s
            manifest["factory_log"] = probe.factory_log

            tmp = path.with_suffix(".part")
            with tarfile.open(tmp, "w:gz") as tar:
                for remote, member, is_dir in R.BACKUP_ITEMS:
                    try:
                        if is_dir:
                            if not c.path_exists(remote):
                                manifest["items"].append(
                                    {"path": remote, "status": "absent"})
                                continue
                            rc, _, err = c.run(
                                "tar -czf /tmp/_vr_bk.tgz -C %s . 2>/dev/null"
                                % R._q(remote), timeout=180)
                            data = c.read_file("/tmp/_vr_bk.tgz", timeout=300)
                            c.run("rm -f /tmp/_vr_bk.tgz")
                        else:
                            if not c.path_exists(remote):
                                manifest["items"].append(
                                    {"path": remote, "status": "absent"})
                                continue
                            data = c.read_file(remote)
                        info = tarfile.TarInfo(member)
                        info.size = len(data)
                        info.mtime = int(time.time())
                        tar.addfile(info, io.BytesIO(data))
                        manifest["items"].append(
                            {"path": remote, "member": member,
                             "bytes": len(data), "status": "ok"})
                    except Exception as e:
                        log.warning("backup item %s failed: %s", remote, e)
                        manifest["items"].append(
                            {"path": remote, "status": "error", "error": str(e)})

                mdata = json.dumps(manifest, indent=2).encode()
                mi = tarfile.TarInfo("manifest.json")
                mi.size = len(mdata)
                mi.mtime = int(time.time())
                tar.addfile(mi, io.BytesIO(mdata))
            tmp.replace(path)

        size = path.stat().st_size
        captured = sum(1 for i in manifest["items"] if i["status"] == "ok")
        store.add_backup(name, size, kind, True, "%d items" % captured)
        store.log_event("info", "backup",
                        "backup ok: %s (%d items, %d bytes)" % (name, captured, size))
        store.kv_set("last_backup_ok", int(time.time()))
        prune_backups(s)
        return {"ok": True, "file": name, "size": size, "items": captured}

    except Exception as e:
        log.exception("backup failed")
        store.log_event("error", "backup", "backup FAILED: %s" % e)
        store.kv_set("last_backup_error", {"ts": int(time.time()), "error": str(e)})
        if s.notify_on_backup_failure:
            notify(s, "backup_failed", "Backup failed: %s" % e)
        try:
            tmp = path.with_suffix(".part")
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return {"ok": False, "error": str(e)}


def prune_backups(s: Settings) -> int:
    """Keep the newest `keep_backups` archives; delete the rest."""
    store.reconcile_backups(BACKUP_DIR)
    rows = store.list_backups()
    doomed = rows[s.keep_backups:]
    n = 0
    for row in doomed:
        p = BACKUP_DIR / row["filename"]
        try:
            if p.exists():
                p.unlink()
            store.forget_backup(row["filename"])
            n += 1
        except OSError as e:
            log.warning("could not delete %s: %s", p, e)
    if n:
        store.log_event("info", "backup", "pruned %d old backup(s)" % n)
    return n


# --------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------
def run_restore(filename: Optional[str] = None, reason: str = "manual") -> dict:
    """
    Reinstall Valetudo and its state onto the robot.

    Order matters: binary -> config -> guard -> boot hook -> start. The boot
    hook is rebuilt from the on-rootfs template so a restore never regresses
    the wifi power-save fix or VALETUDO_CONFIG_PATH.
    """
    s = load_settings()
    store.reconcile_backups(BACKUP_DIR)
    rows = store.list_backups()
    if filename:
        chosen = next((r for r in rows if r["filename"] == filename), None)
    else:
        chosen = rows[0] if rows else None
    if not chosen:
        return {"ok": False, "error": "no backup available to restore"}

    archive = BACKUP_DIR / chosen["filename"]
    if not archive.exists():
        return {"ok": False, "error": "backup file missing: %s" % archive.name}

    steps: list[str] = []
    try:
        binpath = ensure_binary(s)
        blob = binpath.read_bytes()
        import hashlib
        want = hashlib.md5(blob).hexdigest()

        with tarfile.open(archive, "r:gz") as tar:
            def member(name: str) -> Optional[bytes]:
                try:
                    f = tar.extractfile(name)
                    return f.read() if f else None
                except KeyError:
                    return None

            cfg = member("valetudo_config.json")
            guard = member("_wipe_guard.sh")

            with _client(s) as c:
                probe = c.probe()
                if not probe.ssh_ok:
                    raise R.RobotUnreachable(probe.error or "probe failed")

                # 1. binary
                c.write_file(R.P_VALETUDO, blob, mode="0755")
                got = c.md5(R.P_VALETUDO)
                if got != want:
                    raise IOError("md5 mismatch after upload: %s != %s" % (got, want))
                steps.append("binary uploaded + md5 verified")

                # 2. config
                if cfg:
                    c.write_file(R.P_CONFIG, cfg, mode="0600")
                    steps.append("config restored (%d bytes)" % len(cfg))
                else:
                    steps.append("no config in backup - Valetudo will start fresh")

                # 3. wipe guard
                if s.restore_wipe_guard and guard:
                    c.write_file(R.P_GUARD, guard, mode="0755")
                    steps.append("wipe-guard restored")

                # 4. boot hook from the rootfs template
                try:
                    c.rebuild_boot_hook(include_guard=bool(s.restore_wipe_guard and guard))
                    steps.append("boot hook rebuilt from /misc template")
                except Exception as e:
                    steps.append("boot hook rebuild FAILED: %s" % e)

                # 5. start
                c.start_valetudo()
                if s.restore_wipe_guard and guard:
                    c.start_guard()
                steps.append("valetudo started")

        store.log_event("info", "restore",
                        "restore from %s (%s)" % (chosen["filename"], reason), steps)
        if s.notify_on_restore:
            notify(s, "restored",
                   "Valetudo restored from %s" % chosen["filename"],
                   {"reason": reason, "steps": steps})
        store.kv_set("last_restore", {"ts": int(time.time()),
                                      "file": chosen["filename"], "reason": reason})
        return {"ok": True, "file": chosen["filename"], "steps": steps}

    except Exception as e:
        log.exception("restore failed")
        store.log_event("error", "restore", "restore FAILED: %s" % e, steps)
        notify(s, "restore_failed", "Restore failed: %s" % e, {"steps": steps})
        return {"ok": False, "error": str(e), "steps": steps}


# --------------------------------------------------------------------------
# monitoring
# --------------------------------------------------------------------------
def classify(s: Settings) -> tuple[str, R.Probe]:
    try:
        with _client(s) as c:
            p = c.probe()
    except R.RobotAuthError as e:
        p = R.Probe(error="auth: %s" % e)
        return STATE_NO_SSH, p
    except R.RobotUnreachable as e:
        p = R.Probe(error="unreachable: %s" % e)
        return STATE_OFFLINE, p
    except Exception as e:
        p = R.Probe(error=str(e))
        return STATE_NO_SSH, p

    if not p.ssh_ok:
        return STATE_NO_SSH, p
    if not p.binary_present:
        return STATE_WIPED, p
    if not p.valetudo_running:
        return STATE_CRASHED, p
    return STATE_HEALTHY, p


def _attempts_ok(s: Settings) -> bool:
    rec = store.kv_get("restore_attempts", {"n": 0, "t0": 0})
    now = int(time.time())
    if now - rec.get("t0", 0) > s.restore_window_hours * 3600:
        return True
    return rec.get("n", 0) < s.max_restore_attempts


def _attempts_bump(s: Settings) -> None:
    rec = store.kv_get("restore_attempts", {"n": 0, "t0": 0})
    now = int(time.time())
    if now - rec.get("t0", 0) > s.restore_window_hours * 3600:
        rec = {"n": 0, "t0": now}
    rec["n"] = rec.get("n", 0) + 1
    store.kv_set("restore_attempts", rec)


def monitor_tick() -> dict:
    """One monitoring poll. Called on the schedule and from the UI."""
    s = load_settings()
    state, p = classify(s)

    prev = store.kv_get("monitor", {"state": None, "streak": 0})
    streak = prev.get("streak", 0) + 1 if prev.get("state") == state else 1
    store.kv_set("monitor", {
        "state": state, "streak": streak, "ts": int(time.time()),
        "uptime_s": p.uptime_s, "guard_running": p.guard_running,
        "factory_log": p.factory_log, "error": p.error,
    })

    if prev.get("state") != state:
        store.log_event("info", "monitor",
                        "state %s -> %s" % (prev.get("state") or "none", state))

    # Detect a NEW factory-reset entry, independent of the binary check. This
    # is the authoritative signal: the firmware appends a line every time it
    # wipes /data.
    if p.ssh_ok and p.factory_log:
        seen = store.kv_get("factory_log_seen", "")
        if seen and p.factory_log != seen and len(p.factory_log) > len(seen):
            newline = p.factory_log[len(seen):].strip()
            store.log_event("error", "wipe",
                            "NEW factory reset detected: %s" % newline)
            if s.notify_on_wipe:
                notify(s, "wipe_detected",
                       "Robot factory-reset itself: %s" % newline)
        store.kv_set("factory_log_seen", p.factory_log)

    if state == STATE_HEALTHY:
        store.kv_set("restore_attempts", {"n": 0, "t0": 0})
        return {"state": state, "streak": streak, "acted": False}

    if state in (STATE_OFFLINE, STATE_NO_SSH):
        # Never auto-act on an unverified state.
        if streak == s.confirm_samples and state == STATE_NO_SSH:
            store.log_event("warn", "monitor",
                            "cannot reach robot over ssh (%dx) - not acting" % streak)
        return {"state": state, "streak": streak, "acted": False}

    # WIPED or CRASHED - require confirmation before acting.
    if streak < s.confirm_samples:
        return {"state": state, "streak": streak, "acted": False,
                "note": "awaiting confirmation"}

    if streak == s.confirm_samples:
        if state == STATE_WIPED and s.notify_on_wipe:
            notify(s, "wiped", "Valetudo binary is gone - /data was wiped",
                   {"factory_log": p.factory_log})
        elif state == STATE_CRASHED and s.notify_on_crash:
            notify(s, "crashed", "Valetudo is installed but not running")

    if not s.auto_restore:
        return {"state": state, "streak": streak, "acted": False,
                "note": "auto_restore disabled"}

    if not _attempts_ok(s):
        store.log_event("error", "monitor",
                        "recovery budget exhausted (%d/%dh) - human needed"
                        % (s.max_restore_attempts, s.restore_window_hours))
        return {"state": state, "streak": streak, "acted": False,
                "note": "budget exhausted"}

    _attempts_bump(s)
    if state == STATE_CRASHED:
        try:
            with _client(s) as c:
                c.start_valetudo()
                c.start_guard()
            store.log_event("info", "monitor", "restarted Valetudo after crash")
            return {"state": state, "streak": streak, "acted": True,
                    "action": "restart"}
        except Exception as e:
            store.log_event("error", "monitor", "restart failed: %s" % e)
            return {"state": state, "streak": streak, "acted": False,
                    "error": str(e)}

    res = run_restore(reason="auto (%s)" % state)
    return {"state": state, "streak": streak, "acted": True,
            "action": "restore", "result": res}
