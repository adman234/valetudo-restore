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
import threading
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
# --------------------------------------------------------------------------
# backup completeness
# --------------------------------------------------------------------------
# Members every backup of a working robot has. A wipe deletes
# valetudo_config.json, and a backup without the vendor config or the complete
# map set cannot put the robot back the way it was.
CRITICAL_MEMBERS = (["valetudo_config.json", "data_config.tar.gz"]
                    + [m for _, m, _ in R.MAP_PATHS])


def _named_rooms(cfg_tgz: bytes) -> int:
    """Named rooms in ava_SchedulePositionInfo.conf, inside data_config.tar.gz."""
    try:
        with tarfile.open(fileobj=io.BytesIO(cfg_tgz)) as t:
            f = t.extractfile("./ava/ava_SchedulePositionInfo.conf")
            info = json.loads(f.read().decode("utf-8", "replace")) if f else {}
    except Exception:
        return 0
    return sum(1 for r in (info.get("current_name_and_id") or [])
               if isinstance(r, dict) and r.get("name"))


def archive_facts(path: Path) -> dict:
    """What an archive holds, as far as judging its completeness goes."""
    with tarfile.open(path, "r:gz") as tar:
        names = set(tar.getnames())
        manifest = {}
        if "manifest.json" in names:
            try:
                manifest = json.loads(tar.extractfile("manifest.json").read())
            except Exception:
                manifest = {}
        rooms = 0
        if "data_config.tar.gz" in names:
            rooms = _named_rooms(tar.extractfile("data_config.tar.gz").read())
    return {"members": names - {"manifest.json"}, "rooms": rooms,
            "robot": manifest.get("robot_state")}


def assess_backup(facts: dict, baseline: Optional[dict]) -> list[str]:
    """
    Reasons this backup cannot be trusted to put the robot back; [] if it can.

    Deliberately NOT based on archive size or item count, which move for
    legitimate reasons: uninstalling duststreamer took one archive from 18
    items / 3.8 MB to 17 items / 875 KB with nothing wrong, and enabling the
    voice pack adds about 5 MB. These checks look at what a wipe changes.
    """
    reasons = []
    robot = facts.get("robot") or {}
    if robot and not robot.get("binary_present"):
        reasons.append("taken while the robot was wiped (no Valetudo binary)")
    elif robot and not robot.get("config_present"):
        reasons.append("taken while the robot had no Valetudo config")
    missing = [m for m in CRITICAL_MEMBERS if m not in facts["members"]]
    if missing:
        reasons.append("missing %s" % ", ".join(missing))
    if baseline and baseline.get("rooms") and not facts.get("rooms"):
        reasons.append("no named rooms (the last full backup had %d), so the map "
                       "is most likely the placeholder a wipe leaves behind"
                       % baseline["rooms"])
    return reasons


def assess_pending() -> int:
    """
    Give every unassessed backup a verdict: new ones, files adopted from disk,
    and rows from before this check existed. Oldest first, so each is compared
    with the newest full backup that came before it. Returns how many ran.
    """
    cache: dict = {}

    def facts_of(fn: str) -> Optional[dict]:
        if fn not in cache:
            try:
                cache[fn] = archive_facts(BACKUP_DIR / fn)
            except Exception as e:
                log.warning("cannot read %s: %s", fn, e)
                cache[fn] = None
        return cache[fn]

    baseline_fn = None
    n = 0
    for r in reversed(store.list_backups()):
        if r["complete"] is None:
            facts = facts_of(r["filename"])
            if facts is None:
                reasons = ["archive could not be read"]
            else:
                reasons = assess_backup(
                    facts, facts_of(baseline_fn) if baseline_fn else None)
            store.set_assessment(r["filename"], not reasons, reasons)
            r["full"] = bool(r["override"]) or not reasons
            n += 1
        if r["full"]:
            baseline_fn = r["filename"]
    return n


def newest_full(rows: Optional[list] = None) -> Optional[dict]:
    rows = store.list_backups() if rows is None else rows
    return next((r for r in rows if r["full"]), None)


def _no_full_backup_msg(rows: list) -> str:
    if not rows:
        return "no backup available to restore"
    return ("No full backup to restore from: all %d kept backup(s) are flagged "
            "incomplete. Restore a specific one from the table if you are sure "
            "it is good, or mark it as full first." % len(rows))


def _skipped_note(rows: list, chosen: dict) -> Optional[str]:
    newer = rows[:rows.index(chosen)]
    if not newer:
        return None
    return ("skipped %d newer incomplete backup(s): %s"
            % (len(newer), ", ".join(r["filename"] for r in newer)))


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
            # Recorded so the backup can be judged later: one taken while the
            # robot is wiped holds the wipe, not the robot, and must never be
            # the one a restore picks.
            manifest["robot_state"] = {
                "binary_present": probe.binary_present,
                "config_present": probe.config_present,
                "valetudo_running": probe.valetudo_running,
            }

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
        assess_pending()
        row = next((b for b in store.list_backups() if b["filename"] == name), {})
        if row.get("full"):
            store.log_event("info", "backup",
                            "backup ok: %s (%d items, %d bytes)" % (name, captured, size))
        else:
            store.log_event("warn", "backup",
                            "backup %s flagged INCOMPLETE: %s. Restores will skip it."
                            % (name, "; ".join(row.get("reasons") or ["unknown"])))
        store.kv_set("last_backup_ok", int(time.time()))
        prune_backups(s)
        return {"ok": True, "file": name, "size": size, "items": captured,
                "full": bool(row.get("full")), "reasons": row.get("reasons") or []}

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
    assess_pending()
    rows = store.list_backups()
    doomed = rows[s.keep_backups:]
    # Never prune the newest full backup. If the robot sits wiped for longer
    # than the retention window, every nightly backup is incomplete, and a
    # count-based prune would delete the last one that can restore it.
    keeper = newest_full(rows)
    if keeper in doomed:
        doomed.remove(keeper)
        store.log_event("warn", "backup",
                        "kept %s past retention: it is the newest full backup"
                        % keeper["filename"])
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
def run_restore(filename: Optional[str] = None, reason: str = "manual",
                blob: Optional[bytes] = None) -> dict:
    """
    Reinstall Valetudo and its state onto the robot.

    Order matters: binary -> config -> guard -> boot hook -> start. The boot
    hook is rebuilt from the on-rootfs template so a restore never regresses
    the wifi power-save fix or VALETUDO_CONFIG_PATH.
    """
    s = load_settings()
    uploaded_tmp = None
    skipped = None
    if blob is not None:
        # Validate before doing anything: a friendly message beats
        # "not a gzip file" from deep inside tarfile.
        try:
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as probe:
                if "valetudo_config.json" not in probe.getnames():
                    return {"ok": False, "error":
                            "That archive does not look like a backup from this "
                            "tool - it has no valetudo_config.json."}
        except Exception:
            return {"ok": False, "error":
                    "That file is not a readable .tar.gz backup archive. Upload "
                    "a valetudo-backup-....tar.gz produced by this tool."}
        # Restore straight from the upload without adding it to the retention pool.
        import tempfile
        tmpf = tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz")
        tmpf.write(blob); tmpf.close()
        archive = uploaded_tmp = Path(tmpf.name)
        chosen = {"filename": "uploaded archive (%.1f MB)" % (len(blob) / 1048576)}
    else:
        store.reconcile_backups(BACKUP_DIR)
        assess_pending()
        rows = store.list_backups()
        if filename:
            chosen = next((r for r in rows if r["filename"] == filename), None)
        else:
            # "Newest" means newest FULL. A backup taken after a wipe but
            # before a restore captures the wipe, and restoring it would
            # overwrite a recoverable robot with the placeholder state.
            chosen = newest_full(rows)
            if chosen:
                skipped = _skipped_note(rows, chosen)
        if not chosen:
            return {"ok": False, "error": ("backup not found: %s" % filename)
                    if filename else _no_full_backup_msg(rows)}
        archive = BACKUP_DIR / chosen["filename"]
        if not archive.exists():
            return {"ok": False, "error": "backup file missing: %s" % archive.name}

    steps: list[str] = []
    if skipped:
        steps.append(skipped)
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
            guard = member("wifi-keeper.sh")
            voice = member("personalized_voice.tar.gz")
            dust = member("duststreamer")
            vendor_cfg = member("data_config.tar.gz")
            map_have, map_vendor, map_missing = _read_map_members(tar)

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

                # 3. wifi-keeper
                if s.restore_wifi_keeper and guard:
                    c.write_file(R.P_GUARD, guard, mode="0755")
                    steps.append("wifi-keeper restored")

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

                # 3c. vendor settings the user actually chose. These live in
                # /data/config/ava and were previously skipped entirely, which
                # is why obstacle images, pet avoidance and similar came back at
                # factory defaults after a restore. Restored from a curated
                # allowlist - never /data/config/miio (wifi + identity), never
                # the history .db files.
                if s.restore_vendor_settings and vendor_cfg:
                    done, failed = [], []
                    try:
                        with tarfile.open(fileobj=io.BytesIO(vendor_cfg)) as vt:
                            for name in R.VENDOR_SETTINGS:
                                try:
                                    data = vt.extractfile("./ava/" + name).read()
                                except Exception:
                                    continue
                                try:
                                    c.write_file("/data/config/ava/" + name, data)
                                    done.append(name)
                                except Exception:
                                    failed.append(name)
                    except Exception as e:
                        steps.append("vendor settings FAILED: %s" % e)
                    if done:
                        steps.append("vendor settings restored (%d): %s"
                                     % (len(done), ", ".join(done)))
                    if failed:
                        steps.append("vendor settings failed: %s" % ", ".join(failed))

                # 3d. duststreamer - camera streaming binary, wiped with /data
                if s.restore_duststreamer:
                    if dust:
                        c.write_file(R.P_DUSTSTREAMER, dust, mode="0755")
                        steps.append("duststreamer restored (%d bytes)" % len(dust))
                    elif s.duststreamer_url:
                        rc, _, err = c.run(
                            "wget -q -O %s %s && chmod 0755 %s"
                            % (R.P_DUSTSTREAMER, s.duststreamer_url, R.P_DUSTSTREAMER),
                            timeout=300)
                        steps.append("duststreamer downloaded from configured URL"
                                     if rc == 0 else
                                     "duststreamer download FAILED: %s" % err.strip())

                # 4. boot hook from the rootfs template
                try:
                    c.rebuild_boot_hook(include_keeper=bool(s.restore_wifi_keeper and guard))
                    steps.append("boot hook rebuilt from /misc template")
                except Exception as e:
                    steps.append("boot hook rebuild FAILED: %s" % e)

                # 4b. The map, and the ava restart that makes everything above
                # take effect. ava reads /data/config/ava at STARTUP and holds
                # the values in memory, so writing clean_parameter.json under a
                # running ava changes the file but not the robot - pet
                # avoidance, obstacle images and the rest stayed at their old
                # values until it restarted. Stopping ava here does double duty:
                # it is required for the map swap anyway.
                if map_have and "data_map.tar.gz" in map_have and not map_missing:
                    c.stop_map_processes()
                    steps.append("stopped ava + miio_client")
                    try:
                        _swap_map_paths(c, map_have, map_vendor, steps)
                    finally:
                        c.start_map_processes()
                        steps.append("restarted ava + miio_client (settings now applied)")
                elif map_have:
                    steps.append("map NOT restored: archive is missing %s"
                                 % ", ".join(map_missing))
                    # still restart ava so the vendor settings take effect
                    c.stop_map_processes()
                    c.start_map_processes()
                    steps.append("restarted ava + miio_client (settings now applied)")
                else:
                    c.stop_map_processes()
                    c.start_map_processes()
                    steps.append("restarted ava + miio_client (settings now applied)")

                # 5. start - stop any existing instance FIRST. Starting
                # unconditionally leaves the old process running: the newcomer
                # cannot bind port 80 and sits there orphaned, but both have the
                # same VALETUDO_CONFIG_PATH and can write it concurrently.
                if c.stop_valetudo():
                    steps.append("stopped existing Valetudo")
                c.start_valetudo()
                if s.restore_wifi_keeper and guard:
                    # kill any existing guard first; the script also self-checks,
                    # but a stale pidfile should not leave two running
                    c.run("kill $(ps | grep -v grep | grep wifi-keeper.sh "
                          "| awk '{print $1}') 2>/dev/null; rm -f /data/wifi-keeper.pid")
                    c.start_guard()
                steps.append("valetudo started")

        # Record a fresh verdict. The dashboard shows the last recorded one,
        # and without this it said WIPED until the next monitor poll.
        state, after = classify(s)
        record_state(state, after, manual=True)
        steps.append("robot now reports %s" % state)
        # Restarting ava and Valetudo in place has not always been enough for a
        # restore to take, so reboot when asked to. Only a restore that got this
        # far reboots; a failed one never does.
        rebooting = bool(s.reboot_after_restore) and _reboot_after_restore(s, steps, "restore")
        store.log_event("info", "restore",
                        "restore from %s (%s)" % (chosen["filename"], reason), steps)
        if s.notify_on_restore:
            notify(s, "restored",
                   "Valetudo restored from %s" % chosen["filename"],
                   {"reason": reason, "steps": steps})
        store.kv_set("last_restore", {"ts": int(time.time()),
                                      "file": chosen["filename"], "reason": reason})
        return {"ok": True, "file": chosen["filename"], "steps": steps, "state": state,
                "rebooting": rebooting}

    except Exception as e:
        log.exception("restore failed")
        store.log_event("error", "restore", "restore FAILED: %s" % e, steps)
        notify(s, "restore_failed", "Restore failed: %s" % e, {"steps": steps})
        return {"ok": False, "error": str(e), "steps": steps}
    finally:
        if uploaded_tmp is not None:
            try:
                uploaded_tmp.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------
# diagnostics
# --------------------------------------------------------------------------
DIAG_DIR = BACKUP_DIR / "diagnostics"

# What to grab when ava is in trouble. All of this is destroyed by a reboot
# (/tmp) or by the wipe itself (/data/log), which is why three separate
# incidents produced no usable evidence: by the time anyone looked, the crash
# dump was already gone.
DIAG_ITEMS = [
    ("/tmp/log", "tmp_log.tar.gz", True),        # crash dumps + ava's own logs
    ("/data/log", "data_log.tar.gz", True),
    ("/data/wifi-keeper.log", "wifi_keeper.log", False),
    ("/data/ava_reboot_cnt", "ava_reboot_cnt", False),
]


def capture_diagnostics(reason: str, force: bool = False) -> dict:
    """
    Pull ava's crash evidence off the robot before it is destroyed.

    Rate-limited to once every 10 minutes so a crash loop does not fill the
    volume - a single capture is a few hundred KB to a couple of MB.
    """
    s = load_settings()
    last = store.kv_get("last_diag", 0)
    if not force and time.time() - last < 600:
        return {"ok": False, "skipped": "rate-limited (one capture per 10 min)"}

    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    name = "diag-%s.tar.gz" % _stamp()
    path = DIAG_DIR / name
    got, meta = [], {"reason": reason, "created": datetime.now(timezone.utc).isoformat()}
    try:
        with _client(s) as c:
            p = c.probe()
            meta["uptime_s"] = p.uptime_s
            meta["ava_running"] = bool(c.run("pidof ava")[1].strip())
            meta["factory_log"] = p.factory_log
            for cmd, key in (("dmesg | tail -200", "dmesg"),
                             ("ps", "ps"),
                             ("df -h", "df"),
                             ("free -m", "free"),
                             ("ls -la /tmp/log/", "tmp_log_listing")):
                meta[key] = c.run(cmd, timeout=45)[1][-8000:]

            tmp = path.with_suffix(".part")
            with tarfile.open(tmp, "w:gz") as tar:
                for remote, member, is_dir in DIAG_ITEMS:
                    try:
                        if not c.path_exists(remote):
                            continue
                        if is_dir:
                            c.run("tar -czf /tmp/_vr_d.tgz -C %s . 2>/dev/null"
                                  % R._q(remote), timeout=120)
                            data = c.read_file("/tmp/_vr_d.tgz", timeout=180)
                            c.run("rm -f /tmp/_vr_d.tgz")
                        else:
                            data = c.read_file(remote)
                        info = tarfile.TarInfo(member); info.size = len(data)
                        info.mtime = int(time.time())
                        tar.addfile(info, io.BytesIO(data))
                        got.append(member)
                    except Exception as e:
                        log.warning("diag item %s failed: %s", remote, e)
                md = json.dumps(meta, indent=2, default=str).encode()
                mi = tarfile.TarInfo("meta.json"); mi.size = len(md)
                mi.mtime = int(time.time()); tar.addfile(mi, io.BytesIO(md))
            tmp.replace(path)

        store.kv_set("last_diag", int(time.time()))
        store.log_event("info", "diagnostics",
                        "captured %s (%s): %s" % (name, reason, ", ".join(got)))
        # keep the newest 20
        for old in sorted(DIAG_DIR.glob("diag-*.tar.gz"))[:-20]:
            old.unlink(missing_ok=True)
        return {"ok": True, "file": name, "items": got,
                "size": path.stat().st_size}
    except Exception as e:
        log.exception("diagnostic capture failed")
        store.log_event("warn", "diagnostics", "capture FAILED (%s): %s" % (reason, e))
        return {"ok": False, "error": str(e)}


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
        state, after = classify(s)
        record_state(state, after, manual=True)
        store.log_event("info" if back else "error", "restart",
                        "Valetudo restart: %s" % ("ok" if back else "FAILED"), steps)
        return {"ok": back, "steps": steps, "state": state,
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
        _after_reboot(s, "manual reboot")
        return {"ok": True,
                "note": "Reboot requested. The robot takes roughly 2-4 minutes to "
                        "come back; monitoring will report OFFLINE/NO_SSH until "
                        "then, which is expected and will not trigger a restore."}
    except Exception as e:
        store.log_event("error", "reboot", "reboot FAILED: %s" % e)
        return {"ok": False, "error": str(e)}


def _swap_map_paths(c, have, vendor_map, steps) -> str:
    """
    Replace the map paths on the robot. ava and miio_client MUST already be
    stopped by the caller - with ava running it holds the map open and rewrites
    or discards whatever is placed there.

    Returns the quarantine directory the previous map was moved to.
    """
    stamp = _stamp()
    quarantine = "/data/_map_replaced-%s" % stamp
    c.run("mkdir -p %s" % quarantine)
    # keep only the two most recent; /data filling up is itself a wipe trigger
    c.run("ls -d /data/_map_replaced-* 2>/dev/null | head -n -2 | xargs -r rm -rf")

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
                         if rc == 0 else "%s FAILED: %s" % (remote, err.strip()))
        else:
            c.write_file(remote, data)
            steps.append("%s restored (%d bytes)" % (remote, len(data)))

    if vendor_map:
        c.write_file("/data/config/ava/mult_map.json", vendor_map)
        steps.append("mult_map.json restored")
    return quarantine


def _read_map_members(tar):
    """
    Pull the map components + mult_map.json out of an open backup archive.

    MAP_EXTRA_PATHS ride along in `have` but are deliberately excluded from
    `missing`: they are restored when present and skipped when not, so their
    absence can never block a map restore.
    """
    names = set(tar.getnames())
    have = {}
    for remote, member, is_dir in R.MAP_PATHS + R.MAP_EXTRA_PATHS:
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
    missing = [m for _, m, _ in R.MAP_PATHS if m not in have]
    return have, vendor_map, missing


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
            store.reconcile_backups(BACKUP_DIR)
            assess_pending()
            rows = store.list_backups()
            chosen = newest_full(rows)
            if not chosen:
                return {"ok": False, "error": _no_full_backup_msg(rows)}
            filename = chosen["filename"]
            note = _skipped_note(rows, chosen)
            if note:
                steps.append(note)
        p = BACKUP_DIR / filename
        if not p.exists():
            return {"ok": False, "error": "backup not found: %s" % filename}
        blob = p.read_bytes()
        steps.append("source: backup %s" % filename)
    else:
        steps.append("source: uploaded file (%d bytes)" % len(blob))

    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            have, vendor_map, missing = _read_map_members(tar)

        if "data_map.tar.gz" not in have:
            raise ValueError(
                "This archive contains no map data. Only backup archives "
                "produced by this tool can be restored.")
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
            c.stop_map_processes()
            steps.append("stopped ava + miio_client")
            try:
                quarantine = _swap_map_paths(c, have, vendor_map, steps)
            finally:
                c.start_map_processes()
                steps.append("restarted ava + miio_client")

        state, after = classify(s)
        record_state(state, after, manual=True)
        rebooting = bool(s.reboot_after_restore) and _reboot_after_restore(
            s, steps, "map restore")
        store.log_event("info", "restore-map", "map restored", steps)
        return {"ok": True, "steps": steps, "state": state, "rebooting": rebooting,
                "note": "Previous map moved to %s on the robot. %s" % (
                    quarantine,
                    "The robot is rebooting to apply it; check the map in Valetudo "
                    "once it is back, in 2-4 minutes." if rebooting else
                    "Give ava a minute, then check the map in Valetudo.")}
    except Exception as e:
        log.exception("map restore failed")
        store.log_event("error", "restore-map", "map restore FAILED: %s" % e, steps)
        return {"ok": False, "error": str(e), "steps": steps}


# --------------------------------------------------------------------------
# rebooting
# --------------------------------------------------------------------------
# A rebooting robot answers SSH before Valetudo is up, so the confirmation waits
# for HEALTHY (or WIPED) instead of reporting the first answer it gets.
REBOOT_FIRST_CHECK_S = 90
REBOOT_POLL_S = 20
REBOOT_GIVE_UP_S = 600


def _spawn(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True,
                     name="vr-" + fn.__name__).start()


def _after_reboot(s: Settings, what: str) -> None:
    """
    The robot was just told to reboot. Show it as OFFLINE (it is about to be)
    rather than leaving the pre-reboot verdict on the dashboard, and confirm in
    the background when it is back.
    """
    record_state(STATE_OFFLINE, R.Probe(error="rebooting after the %s" % what),
                 manual=True)
    _spawn(_confirm_back_after_reboot, s, what)


def _reboot_after_restore(s: Settings, steps: list, what: str) -> bool:
    """Reboot after a successful restore. Never raises; returns True if requested."""
    try:
        with _client(s) as c:
            c.reboot()
    except Exception as e:
        steps.append("reboot after the %s FAILED: %s" % (what, e))
        store.log_event("error", "reboot", "reboot after the %s FAILED: %s" % (what, e))
        return False
    steps.append("rebooting the robot so the %s takes effect (back in 2-4 minutes)" % what)
    store.log_event("info", "reboot", "rebooting the robot after the %s" % what)
    _after_reboot(s, what)
    return True


def _confirm_back_after_reboot(s: Settings, what: str,
                               first_check: int = REBOOT_FIRST_CHECK_S,
                               poll: int = REBOOT_POLL_S,
                               give_up: int = REBOOT_GIVE_UP_S) -> str:
    """Wait for the robot to come back and record how it came back."""
    time.sleep(first_check)
    waited = first_check
    while True:
        state, p = classify(s)
        done = state in (STATE_HEALTHY, STATE_WIPED)
        if done or waited >= give_up:
            record_state(state, p, manual=True)
            if state == STATE_HEALTHY:
                store.log_event("info", "reboot", "robot back %ds after the reboot "
                                "(%s): HEALTHY" % (waited, what))
            elif done:
                store.log_event("error", "reboot", "robot came back %ds after the "
                                "reboot (%s) %s" % (waited, what, state))
            else:
                store.log_event("error", "reboot", "robot still %s %ds after the "
                                "reboot (%s); monitoring carries on" % (state, waited, what))
            return state
        time.sleep(poll)
        waited += poll


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


def record_state(state: str, p: R.Probe, manual: bool = False) -> tuple[dict, int]:
    """
    Store the verdict the dashboard shows. Every real probe goes through here:
    the scheduled monitor, Test connection, and the check after a restore or
    restart. Previously only the monitor wrote it, so after a restore the card
    kept saying WIPED until the next poll.

    A manual probe updates what is shown but never advances the confirmation
    streak, so pressing Test connection cannot make auto-restore act sooner or
    skip past the sample where the wipe notification fires.
    """
    prev = store.kv_get("monitor", {"state": None, "streak": 0})
    same = prev.get("state") == state
    if manual:
        streak = prev.get("streak", 0) if same else 0
    else:
        streak = prev.get("streak", 0) + 1 if same else 1
    store.kv_set("monitor", {
        "state": state, "streak": streak, "ts": int(time.time()),
        "uptime_s": p.uptime_s, "guard_running": p.guard_running,
        "factory_log": p.factory_log, "error": p.error,
        "source": "manual" if manual else "monitor",
    })
    if not same:
        store.log_event("info", "monitor", "state %s -> %s%s" % (
            prev.get("state") or "none", state, " (manual check)" if manual else ""))
        # Grab the evidence NOW, on the first sighting from any source, while
        # /tmp still holds the crash dump. A reboot clears /tmp and a wipe
        # clears /data/log, so waiting for confirmation means capturing nothing.
        if state == STATE_CRASHED:
            capture_diagnostics("ava crashed (%s saw CRASHED)"
                                % ("manual check" if manual else "monitor"))
    return prev, streak


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


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
    _prev, streak = record_state(state, p)

    # Detect a NEW factory-reset entry, independent of the binary check. The
    # log lives in /data/log, so every wipe deletes it and the firmware starts
    # a fresh one: it only ever holds the LATEST entry. Compare the entry, not
    # the length. A length check caught the Sep 11 wipe only because "Sep 11"
    # is one character longer than "Sep 1", and would miss a wipe on Sep 12.
    if p.ssh_ok and p.factory_log:
        seen = store.kv_get("factory_log_seen", "")
        newline = _last_line(p.factory_log)
        if seen and newline != _last_line(seen):
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
