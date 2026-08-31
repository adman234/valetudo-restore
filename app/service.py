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
def _webhook_parts(s: Settings):
    headers = {"Content-Type": "application/json"}
    err = None
    if s.webhook_headers.strip():
        try:
            headers.update(json.loads(s.webhook_headers))
        except Exception as e:
            err = "webhook_headers is not valid JSON: %s" % e
            log.warning(err)
    return headers, err


def notify(s: Settings, event: str, message: str, detail: Optional[dict] = None) -> None:
    if not s.webhook_url:
        return
    headers, _ = _webhook_parts(s)
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


def test_webhook(s: Optional[Settings] = None) -> dict:
    """
    Fire a test notification and report exactly what happened.

    Returns the HTTP status rather than a bare ok/fail: Home Assistant answers
    404 for a webhook id that does not exist, which is the single most common
    misconfiguration and is invisible if you only check "did it throw".
    """
    s = s or load_settings()
    if not s.webhook_url:
        return {"ok": False, "error": "No webhook URL configured."}
    headers, hdr_err = _webhook_parts(s)
    if hdr_err:
        return {"ok": False, "error": hdr_err}

    payload = {
        "source": "valetudo-restore",
        "event": "test",
        "message": "Test notification from valetudo-restore. "
                   "If you can see this, notifications are working.",
        "robot": s.robot_host,
        "ts": int(time.time()),
        "detail": {"test": True},
    }
    started = time.time()
    try:
        r = httpx.post(s.webhook_url, json=payload, headers=headers, timeout=10.0)
    except httpx.ConnectError as e:
        store.log_event("warn", "notify", "webhook test failed: %s" % e)
        return {"ok": False, "error": "Could not connect: %s" % e,
                "hint": "Check the host/port is reachable from inside the container."}
    except httpx.TimeoutException:
        store.log_event("warn", "notify", "webhook test timed out")
        return {"ok": False, "error": "Timed out after 10s."}
    except Exception as e:
        store.log_event("warn", "notify", "webhook test failed: %s" % e)
        return {"ok": False, "error": str(e)}

    ms = int((time.time() - started) * 1000)
    body = (r.text or "")[:300]
    out = {"ok": r.is_success, "status": r.status_code, "ms": ms, "body": body,
           "url": s.webhook_url}
    if not r.is_success:
        if r.status_code == 404:
            out["hint"] = ("404 - Home Assistant returns this when the webhook id "
                           "does not exist. Check the automation's trigger id "
                           "matches the URL.")
        elif r.status_code in (401, 403):
            out["hint"] = "Rejected. If the endpoint needs a token, add it under Extra headers."
        elif r.status_code >= 500:
            out["hint"] = "The receiving server errored. The request did arrive."
    store.log_event("info" if r.is_success else "warn", "notify",
                    "webhook test -> HTTP %s (%dms)" % (r.status_code, ms))
    return out


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

            items = list(R.BACKUP_ITEMS)
            if s.backup_voice_pack:
                items.append(R.OPTIONAL_ITEMS["voice_pack"])

            tmp = path.with_suffix(".part")
            with tarfile.open(tmp, "w:gz") as tar:
                for remote, member, is_dir in items:
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
            voice = member("personalized_voice.tar.gz")

            with _client(s) as c:
                probe = c.probe()
                if not probe.ssh_ok:
                    raise R.RobotUnreachable(probe.error or "probe failed")

                # 1. binary - skip the 37 MB upload if it is already correct.
                # Most restores after a crash (as opposed to a wipe) have an
                # intact binary, and the upload is the single most failure-prone
                # step over this robot's wifi.
                existing = c.md5(R.P_VALETUDO) if c.path_exists(R.P_VALETUDO) else ""
                if existing == want:
                    steps.append("binary already correct (md5 match) - upload skipped")
                else:
                    steps.append("uploading binary (%.1f MB)..." % (len(blob) / 1048576))
                    c.write_file(R.P_VALETUDO, blob, mode="0755")
                    got = c.md5(R.P_VALETUDO)
                    if got != want:
                        raise IOError("md5 mismatch after upload: %s != %s" % (got, want))
                    steps[-1] = "binary uploaded + md5 verified"


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

                # 3b. voice pack - user-installed audio a wipe destroys.
                # /data/config/ava/language_in_use (restored with the vendor
                # config) names the pack; without these files the robot would
                # reference a pack that no longer exists.
                if voice:
                    c.write_file("/tmp/_vr_voice.tgz", voice)
                    rc, _, err = c.run(
                        "mkdir -p %s && tar -xzf /tmp/_vr_voice.tgz -C %s && "
                        "rm -f /tmp/_vr_voice.tgz" % (R.P_VOICE, R.P_VOICE),
                        timeout=300)
                    steps.append("voice pack restored (%d bytes)" % len(voice)
                                 if rc == 0 else
                                 "voice pack FAILED: %s" % err.strip())

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
def restart_valetudo() -> dict:
    """
    Stop and restart the Valetudo process on the robot.

    Valetudo's own web UI has no restart control, and the robot has no service
    manager, so this stops the process and relaunches it the same way the boot
    hook does - crucially with VALETUDO_CONFIG_PATH set. Restarting it without
    that env var would silently move its config to tmpfs, and every setting
    would vanish at the next reboot.
    """
    s = load_settings()
    steps: list[str] = []
    try:
        with _client(s) as c:
            p = c.probe()
            if not p.ssh_ok:
                raise R.RobotUnreachable(p.error or "probe failed")
            if not p.binary_present:
                return {"ok": False,
                        "error": "/data/valetudo is missing - the robot was wiped. "
                                 "Use Restore, not Restart."}
            steps.append("was running" if p.valetudo_running else "was not running")
            if p.valetudo_running and not c.stop_valetudo():
                raise IOError("Valetudo would not stop")
            steps.append("stopped")
            c.start_valetudo()
            steps.append("started with VALETUDO_CONFIG_PATH=%s" % R.P_CONFIG)

            # confirm it actually came back rather than assuming
            back = False
            for _ in range(20):
                c.run("sleep 1")
                rc, out, _ = c.run("pidof valetudo")
                if rc == 0 and out.strip():
                    back = True
                    break
            steps.append("running (pid %s)" % out.strip() if back
                         else "did NOT come back")
        store.log_event("info" if back else "error", "restart",
                        "Valetudo restart: %s" % ("ok" if back else "FAILED"), steps)
        return {"ok": back, "steps": steps,
                "note": None if back else
                        "Valetudo did not restart. Check the binary and config."}
    except Exception as e:
        log.exception("restart failed")
        store.log_event("error", "restart", "restart FAILED: %s" % e, steps)
        return {"ok": False, "error": str(e), "steps": steps}


def reboot_robot() -> dict:
    """Reboot the whole robot (not just Valetudo)."""
    s = load_settings()
    try:
        with _client(s) as c:
            p = c.probe()
            if not p.ssh_ok:
                raise R.RobotUnreachable(p.error or "probe failed")
            c.reboot()
        store.log_event("info", "reboot", "robot reboot requested")
        return {"ok": True,
                "note": "Reboot requested. The robot takes roughly 2-4 minutes to "
                        "come back; monitoring will report OFFLINE/NO_SSH until "
                        "then, which is expected and will not trigger a restore."}
    except Exception as e:
        store.log_event("error", "reboot", "reboot FAILED: %s" % e)
        return {"ok": False, "error": str(e)}


def restore_map(blob: Optional[bytes] = None, filename: Optional[str] = None,
                force: bool = False) -> dict:
    """
    Restore a complete map, the way pkoehlers/maploader does it.

    A map is NOT just /data/map. For this model (r2491 / Mova P10 Pro Ultra) it
    is /data/ri + /data/map + /data/DivideMap + /data/config/ava/mult_map.json,
    and the sibling r2416 profile adds /data/DivideDebug and
    /data/log/map_info.bin. Restoring only /data/map leaves an incomplete map
    that ava discards on the next boot - which is exactly what happened
    repeatedly before this was understood.

    ava and miio_client are stopped for the swap rather than rebooting: with ava
    running it holds the map open and rewrites or discards whatever is placed
    there.

    /data/config/miio is never touched (wifi + device identity), nor
    /mnt/private.
    """
    s = load_settings()
    steps: list[str] = []

    if blob is None:
        if not filename:
            rows = store.list_backups()
            if not rows:
                return {"ok": False, "error": "no backup available"}
            filename = rows[0]["filename"]
        p = BACKUP_DIR / filename
        if not p.exists():
            return {"ok": False, "error": "backup not found: %s" % filename}
        blob = p.read_bytes()
        steps.append("source: backup %s" % filename)
    else:
        steps.append("source: uploaded file (%d bytes)" % len(blob))

    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            names = set(tar.getnames())
            have = {}
            for remote, member, is_dir in R.MAP_PATHS:
                if member in names:
                    have[member] = (remote, tar.extractfile(member).read(), is_dir)
            vendor_map = None
            if "data_config.tar.gz" in names:
                cfg = tar.extractfile("data_config.tar.gz").read()
                with tarfile.open(fileobj=io.BytesIO(cfg)) as inner:
                    try:
                        vendor_map = inner.extractfile("./ava/mult_map.json").read()
                    except Exception:
                        vendor_map = None

        if "data_map.tar.gz" not in have:
            raise ValueError(
                "This archive contains no map data. Only backup archives "
                "produced by this tool can be restored.")

        # Refuse an incomplete archive rather than producing a map ava will
        # reject. Archives taken before the full map set was understood have
        # /data/map but not /data/ri or /data/DivideMap; restoring those looks
        # like it works and then the map silently vanishes on the next boot.
        missing = [m for _, m, _ in R.MAP_PATHS if m not in have]
        if missing and not force:
            return {
                "ok": False,
                "error": "This backup is missing %s, so the map is incomplete "
                         "and ava would discard it on the next boot. It was "
                         "taken before this tool captured the full map set. "
                         "Use a newer backup." % ", ".join(missing),
                "missing": missing,
                "hint": "Pass force=true to attempt it anyway (expected to fail).",
            }
        if missing:
            steps.append("FORCED: archive missing %s - expect ava to discard "
                         "the map" % ", ".join(missing))

        with _client(s) as c:
            probe = c.probe()
            if not probe.ssh_ok:
                raise R.RobotUnreachable(probe.error or "probe failed")

            stamp = _stamp()
            quarantine = "/data/_map_replaced-%s" % stamp
            c.run("mkdir -p %s" % quarantine)
            # Keep only the two most recent replaced maps; these are a few MB
            # each and /data filling up is itself a factory-reset trigger.
            c.run("ls -d /data/_map_replaced-* 2>/dev/null | head -n -2 "
                  "| xargs -r rm -rf")

            c.stop_map_processes()
            steps.append("stopped ava + miio_client")
            try:
                for member, (remote, data, is_dir) in have.items():
                    c.run("chattr -R -i %s 2>/dev/null" % R._q(remote))
                    c.run("[ -e %s ] && mv %s %s/ 2>/dev/null"
                          % (R._q(remote), R._q(remote), quarantine))
                    if is_dir:
                        c.run("mkdir -p %s" % R._q(remote))
                        c.write_file("/tmp/_vr_m.tgz", data)
                        rc, _, err = c.run(
                            "tar -xzf /tmp/_vr_m.tgz -C %s && rm -f /tmp/_vr_m.tgz"
                            % R._q(remote), timeout=180)
                        steps.append("%s restored (%d bytes)" % (remote, len(data))
                                     if rc == 0 else
                                     "%s FAILED: %s" % (remote, err.strip()))
                    else:
                        c.write_file(remote, data)
                        steps.append("%s restored (%d bytes)" % (remote, len(data)))

                if vendor_map:
                    c.write_file("/data/config/ava/mult_map.json", vendor_map)
                    steps.append("mult_map.json restored")
            finally:
                c.start_map_processes()
                steps.append("restarted ava + miio_client")

        store.log_event("info", "restore-map", "map restored", steps)
        return {"ok": True, "steps": steps,
                "note": "Previous map moved to %s on the robot. Give ava a minute, "
                        "then check the map in Valetudo." % quarantine}
    except Exception as e:
        log.exception("map restore failed")
        store.log_event("error", "restore-map", "map restore FAILED: %s" % e, steps)
        return {"ok": False, "error": str(e), "steps": steps}


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
