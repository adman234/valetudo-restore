"""
SSH client for Dreame/Mova robots running Valetudo.

Everything in here encodes constraints discovered on a real Mova P10 Pro Ultra
(r2416, Athena Linux, BusyBox userland):

* There is NO sftp-server on the robot, so scp/sftp fail with
  "sh: /usr/libexec/sftp-server: not found". Files must be streamed through
  `cat > dest` over an exec channel instead.
* The rootfs is a read-only squashfs. Only /data (and the tiny /mnt/misc) are
  writable, which is exactly why a /data wipe destroys Valetudo.
* Shell scripts pushed to the robot MUST have LF line endings. BusyBox ash
  fails on CRLF with `syntax error: unexpected end of file (expecting "then")`
  and the script silently never runs.
* Classification must never infer "wiped" from a failure to observe. An auth
  failure or a dropped wifi link is not evidence that a file is gone.
"""
from __future__ import annotations

import io
import logging
import re
from pathlib import Path
import socket
from dataclasses import dataclass
from typing import Optional

import paramiko

log = logging.getLogger("vr.robot")

# Written as byte constants rather than inline literals: these get mangled far
# too easily by tooling that rewrites this file.
CRLF = b"\r\n"
LF = b"\n"

# Paths on the robot
P_VALETUDO = "/data/valetudo"
P_CONFIG = "/data/valetudo_config.json"
P_GUARD = "/data/wifi-keeper.sh"
P_POSTBOOT = "/data/_root_postboot.sh"
P_POSTBOOT_TPL = "/misc/_root_postboot.sh.tpl"
P_FACTORY_LOG = "/data/log/factory_reset.log"
P_CRASH_KEEPER = "/data/crash-keeper.sh"
P_EVIDENCE = "/mnt/misc/vr-evidence"
# DISARM=1|0: whether crash-keeper clears the watchdog's strike mark
P_CK_CONF = "/data/crash-keeper.conf"
# The robot's miIO device token: the key to its local command channel
# (app/miio.py). Read when needed, never stored or logged.
P_MIIO_TOKEN = "/data/config/miio/device.token"

# The line laser adds carpet outlines to the map. Its node reads a read-only rootfs file, so the
# override is a copy with the two switches off, bind-mounted over the original. It has to be
# redone on every boot (the mount is gone, and a wipe or a restore rebuilds the hook), hence a
# block in the boot hook. ava is started before the hook runs and may already have read the
# original, so the block restarts it once after mounting.
LASER_CONF = "/ava/conf/laser_transfer_conf.json"
LASER_CONF_COPY = "/tmp/laser_transfer_conf.json"
_LASER_SED = ("sed -e 's/\"carpet_inspect\": *true/\"carpet_inspect\": false/' "
              "-e 's/\"carpet_fine\": *true/\"carpet_fine\": false/' %s > %s"
              % (LASER_CONF, LASER_CONF_COPY))
OVERRIDE_BEGIN = "# >>> carpet-overrides (valetudo-restore) >>>"
OVERRIDE_END = "# <<< carpet-overrides (valetudo-restore) <<<"
LASER_OVERRIDE_BLOCK = OVERRIDE_BEGIN + """
# The line laser adds carpet outlines to the map. The node reads the read-only rootfs file
# /ava/conf/laser_transfer_conf.json, so a copy with carpet_inspect and carpet_fine set to false
# is bind-mounted over it, rebuilt from the original on every boot. ava is started before this
# hook runs and may already have read the original, so it is restarted once after the mount.
if [ -f /ava/conf/laser_transfer_conf.json ] && ! grep -q '"carpet_inspect": false' /ava/conf/laser_transfer_conf.json; then
        if """ + _LASER_SED + """ \\
                && grep -q '"carpet_inspect": false' """ + LASER_CONF_COPY + """ \\
                && mount --bind """ + LASER_CONF_COPY + " " + LASER_CONF + """; then
                echo "[$(cut -d' ' -f1 /proc/uptime)] laser override mounted" >> /tmp/carpet-overrides.log
                if pidof ava > /dev/null; then
                        sh /etc/rc.d/miio.sh stop > /dev/null 2>&1
                        killall ava
                        for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do pidof ava > /dev/null || break; sleep 1; done
                        pidof ava > /dev/null && killall -9 ava
                        sh /etc/rc.d/ava.sh > /dev/null 2>&1 &
                        sh /etc/rc.d/miio.sh > /dev/null 2>&1 &
                        echo "[$(cut -d' ' -f1 /proc/uptime)] ava restarted" >> /tmp/carpet-overrides.log
                fi
        fi
fi
""" + OVERRIDE_END + "\n"

# What an earlier hand-installed version of the block looked like: it carried no markers.
_OLD_OVERRIDE = re.compile(
    r"\n?# laser carpet inspection off \(valetudo-restore experiment\).*?\nfi\n", re.S)
_NEW_OVERRIDE = re.compile(
    r"\n?" + re.escape(OVERRIDE_BEGIN) + r".*?" + re.escape(OVERRIDE_END) + r"[^\n]*\n", re.S)


def hook_with_laser_override(text: str) -> str:
    """
    The boot hook text with the laser override block in it, once, at the end (after
    Valetudo and the keepers start, because the block can wait up to 15 s for ava to
    exit). Any older copy of the block is replaced. Idempotent.
    """
    base = _NEW_OVERRIDE.sub("\n", _OLD_OVERRIDE.sub("\n", text)).rstrip("\n")
    return base + "\n\n" + LASER_OVERRIDE_BLOCK

# Helper scripts ship inside the image (guard/ at the repo root), so a restore
# can install the current version even onto a robot whose backup never had it.
GUARD_DIR = Path(__file__).resolve().parent.parent / "guard"


def bundled(name: str) -> bytes:
    return (GUARD_DIR / name).read_bytes()

# A "map" on these robots is NOT just /data/map. Per pkoehlers/maploader, which
# supports this exact model (r2491, Mova P10 Pro Ultra), it is:
#
#     MapFolders: /data/ri, /data/map, /data/DivideMap
#     mapFiles:   /data/config/ava/mult_map.json
#
# and the related r2416 (Dreame X40) profile adds /data/DivideDebug and
# /data/log/map_info.bin. This robot reports model mova.vacuum.r2491a but has
# hostname r2416_release, so we capture the UNION of both - they are tiny.
#
# Backing up only /data/map is why earlier restores failed: ava treats an
# incomplete map as invalid and discards the slot on the next boot.
MAP_PATHS = [
    ("/data/ri", "data_ri.tar.gz", True),
    ("/data/map", "data_map.tar.gz", True),
    ("/data/DivideMap", "data_dividemap.tar.gz", True),
    ("/data/DivideDebug", "data_dividedebug.tar.gz", True),
    ("/data/log/map_info.bin", "map_info.bin", False),
]

# duststreamer is the camera-streaming binary. Like Valetudo it lives on /data
# and is destroyed by a wipe, and like Valetudo it is a single static binary -
# so it is captured when present and put back on restore.
# Map-scoped data restored ALONGSIDE the map but not required for it to be
# considered complete. /data/DivideAI/ai_result/<slot>/ai_floors_large.txt holds
# the per-room floor material the robot detected ("wood", "ceramic",
# "shorthaired_carpet"). The numeric per-room material in /data/ri survives a
# restore on its own, but without this the detection layer comes back empty and
# rooms read as generic until the robot re-derives it on a later clean.
MAP_EXTRA_PATHS = [
    ("/data/DivideAI", "data_divideai.tar.gz", True),
]

DUSTSTREAMER_ITEM = ("/data/duststreamer", "duststreamer", False)

# What a backup captures: (remote_path, archive_member_name, is_dir)
BACKUP_ITEMS = [
    ("/data/valetudo_config.json", "valetudo_config.json", False),
    ("/data/wifi-keeper.sh", "wifi-keeper.sh", False),
    ("/data/_root_postboot.sh", "_root_postboot.sh", False),
    ("/data/log/factory_reset.log", "factory_reset.log", False),
    ("/data/config", "data_config.tar.gz", True),
    ("/mnt/misc", "misc.tar.gz", True),
    ("/mnt/private", "private.tar.gz", True),
    # Small state files that hold real user-facing settings and bookkeeping.
    # /data/misc is deliberately NOT here: it is a byte-identical copy of
    # /mnt/misc, which is already captured.
    ("/data/sys_info_record.json", "sys_info_record.json", False),   # charging window, mop/waterbox
    ("/data/clean_record.json", "clean_record.json", False),         # consumable counters
    ("/data/ai_model_inuse.json", "ai_model_inuse.json", False),     # AI model versions
    ("/data/zt_conmon_file", "zt_conmon_file.tar.gz", True),         # robot state + map bookkeeping
] + MAP_PATHS + MAP_EXTRA_PATHS + [DUSTSTREAMER_ITEM]

# Optional extras, controlled by settings. The voice pack is user-installed
# content of a few MB: /data/config/ava/language_in_use records WHICH pack is
# selected (and is inside data_config.tar.gz), but the audio itself lives in
# /data/personalized_voice and is lost in a wipe. Without the original download
# URL it cannot be regenerated, so it is worth capturing.
OPTIONAL_ITEMS = {
    "voice_pack": ("/data/personalized_voice", "personalized_voice.tar.gz", True),
}


P_VOICE = "/data/personalized_voice"
P_DUSTSTREAMER = "/data/duststreamer"

# Vendor settings that hold USER choices and are safe to put back after a wipe.
# Deliberately a curated allowlist rather than all of /data/config:
#   * /data/config/miio is never restored - wifi + device identity
#   * the .db files (clean_log.db, timer_task.db) are history, and history is
#     the most likely place for the corruption that triggers a wipe
# clean_parameter.json is the important one: obstacle images, pet avoidance,
# carpet handling, child lock, mop settings all live there.
VENDOR_SETTINGS = [
    "clean_parameter.json",           # obstacle images, pet avoidance, carpet, mop, child lock
    "cd_conf.json",                   # auto-empty behaviour
    "annoy.json",
    "audio.conf",                     # WHICH voice pack is installed (+ its md5)
    "language_in_use",                # which voice pack is selected
    "media.conf",
    "ava_speech.conf",
    "ava_speech_seginfo.conf",
    "ava_SchedulePositionInfo.conf",  # room names
    "ava_shortcutinfo.conf",
    "iot_conf.json",
]


class RobotAuthError(Exception):
    """Key rejected / auth failed - explicitly NOT evidence of a wipe."""


class RobotUnreachable(Exception):
    """Network-level failure - explicitly NOT evidence of a wipe."""


@dataclass
class Probe:
    """Result of inspecting the robot. `ssh_ok` gates every other field."""

    ssh_ok: bool = False
    binary_present: bool = False
    config_present: bool = False
    guard_running: bool = False
    factory_log: str = ""
    uptime_s: int = 0
    valetudo_running: bool = False
    # crash-keeper's wipe guard: strikes it has disarmed (ever), whether it is
    # standing down after too many in a row, and its latest strikes.log line
    wipes_prevented: int = 0
    guard_standing_down: bool = False
    last_strike: str = ""
    error: str = ""

    @property
    def wiped(self) -> bool:
        # Only meaningful when ssh_ok. A wipe removes the binary from /data.
        return self.ssh_ok and not self.binary_present


def _q(path: str) -> str:
    """Single-quote a path for the remote shell."""
    return "'" + path.replace("'", "'\"'\"'") + "'"


class RobotClient:
    def __init__(self, host: str, port: int, user: str, key_path: str,
                 timeout: int = 10):
        self.host = host
        self.port = port
        self.user = user
        self.key_path = key_path
        self.timeout = timeout
        self._c: Optional[paramiko.SSHClient] = None

    # ---------- connection ----------
    def __enter__(self) -> "RobotClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        c = paramiko.SSHClient()
        # The robot's host key changes after every factory reset, so pinning it
        # would break at exactly the moment recovery is needed most.
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            key = self._load_key()
        except Exception as e:
            raise RobotAuthError("cannot load key %s: %s" % (self.key_path, e)) from e
        try:
            c.connect(
                hostname=self.host,
                port=self.port,
                username=self.user,
                pkey=key,
                timeout=self.timeout,
                banner_timeout=self.timeout,
                auth_timeout=self.timeout,
                allow_agent=False,
                look_for_keys=False,
            )
        except paramiko.AuthenticationException as e:
            raise RobotAuthError("authentication rejected: %s" % e) from e
        except (socket.timeout, socket.error, paramiko.SSHException) as e:
            raise RobotUnreachable(
                "cannot reach %s:%s: %s" % (self.host, self.port, e)
            ) from e
        # Keepalives matter: a multi-megabyte upload over this robot's wifi can
        # otherwise have the transport torn down mid-transfer, which surfaces as
        # the singularly unhelpful "Socket is closed".
        try:
            c.get_transport().set_keepalive(15)
        except Exception:
            pass
        self._c = c

    def _load_key(self):
        with open(self.key_path, "r", encoding="utf-8") as fh:
            data = fh.read()
        last = None
        for cls in (paramiko.RSAKey, paramiko.Ed25519Key,
                    paramiko.ECDSAKey, paramiko.DSSKey):
            try:
                return cls.from_private_key(io.StringIO(data))
            except Exception as e:  # try the next key type
                last = e
        raise last or ValueError("unrecognised key format")

    def close(self) -> None:
        if self._c:
            try:
                self._c.close()
            finally:
                self._c = None

    # ---------- primitives ----------
    def run(self, cmd: str, timeout: int = 30):
        assert self._c, "not connected"
        _, out, err = self._c.exec_command(cmd, timeout=timeout)
        so = out.read().decode("utf-8", "replace")
        se = err.read().decode("utf-8", "replace")
        return out.channel.recv_exit_status(), so, se

    def read_file(self, path: str, timeout: int = 120) -> bytes:
        """Read a remote file as bytes (via cat - no sftp on this device)."""
        assert self._c, "not connected"
        _, out, err = self._c.exec_command("cat " + _q(path), timeout=timeout)
        data = out.read()
        rc = out.channel.recv_exit_status()
        if rc != 0:
            msg = err.read().decode("utf-8", "replace").strip()
            raise FileNotFoundError("%s: %s" % (path, msg))
        return data

    def write_file(self, path: str, data: bytes, mode: str = "0644",
                   timeout: int = 900, retries: int = 3,
                   progress=None) -> None:
        """
        Stream bytes to a remote path.

        scp/sftp are unavailable on this robot, so this pipes through `cat >`.
        Shell scripts are normalised to LF, because BusyBox ash cannot parse
        CRLF and fails in a way that looks like the script simply never ran.

        Written in chunks and retried: a 37 MB upload takes tens of seconds and
        this robot's wifi roams between access points, so a single dropped
        transport should not fail the whole restore. Paramiko reports such a
        drop as "Socket is closed", which says nothing about where it happened -
        hence the explicit progress/attempt reporting.
        """
        if path.endswith(".sh") and CRLF in data:
            log.warning("normalising CRLF -> LF for %s", path)
            data = data.replace(CRLF, LF)

        chunk = 262144
        last_err = None
        for attempt in range(1, retries + 1):
            try:
                tr = self._c.get_transport() if self._c else None
                if tr is None or not tr.is_active():
                    log.warning("transport dead before write, reconnecting")
                    self.close()
                    self.connect()
                chan = self._c.get_transport().open_session(timeout=self.timeout)
                chan.settimeout(timeout)
                chan.exec_command("cat > " + _q(path))
                sent = 0
                try:
                    while sent < len(data):
                        n = chan.sendall(data[sent:sent + chunk])
                        sent += chunk
                        if progress and sent % (chunk * 20) == 0:
                            progress(min(sent, len(data)), len(data))
                finally:
                    chan.shutdown_write()
                rc = chan.recv_exit_status()
                chan.close()
                if rc != 0:
                    raise IOError("remote cat exited %s writing %s" % (rc, path))
                self.run("chmod %s %s" % (mode, _q(path)))
                return
            except Exception as e:
                last_err = e
                log.warning("write %s attempt %d/%d failed: %s",
                            path, attempt, retries, e)
                try:
                    self.close()
                    self.connect()
                except Exception:
                    pass
        raise IOError("write %s failed after %d attempts: %s"
                      % (path, retries, last_err))

    def md5(self, path: str) -> str:
        rc, out, _ = self.run("md5sum " + _q(path))
        if rc != 0 or not out.strip():
            return ""
        return out.split()[0].lower()

    def path_exists(self, path: str) -> bool:
        return self.run("[ -e %s ]" % _q(path))[0] == 0

    # ---------- inspection ----------
    def probe(self) -> Probe:
        """
        One round-trip that reports everything, with a sentinel.

        The sentinel is the whole point: without it we cannot distinguish
        "ssh worked and the binary is missing" (a real wipe) from "ssh did not
        answer" (wifi blip, bad key, robot booting). Conflating those produces
        false WIPED verdicts and spurious auto-restores.
        """
        script = (
            "echo __VR_OK__; "
            "[ -x %s ] && echo BIN=1 || echo BIN=0; "
            "[ -f %s ] && echo CFG=1 || echo CFG=0; "
            "ps | grep -v grep | grep -q wifi-keeper.sh && echo GUARD=1 || echo GUARD=0; "
            "pidof valetudo >/dev/null && echo VAL=1 || echo VAL=0; "
            "echo UP=$(cut -d. -f1 /proc/uptime); "
            "echo CKP=$(cat %s/.prevented 2>/dev/null); "
            "[ -e %s/.gaveup ] && echo CKG=1 || echo CKG=0; "
            "echo CKL=$(tail -n 1 %s/strikes.log 2>/dev/null); "
            "echo __FRLOG__; cat %s 2>/dev/null"
        ) % (P_VALETUDO, P_CONFIG, P_EVIDENCE, P_EVIDENCE, P_EVIDENCE, P_FACTORY_LOG)

        rc, out, err = self.run(script, timeout=self.timeout + 20)
        p = Probe()
        if "__VR_OK__" not in out:
            p.error = (err or out or "no sentinel in response").strip()[:400]
            return p
        p.ssh_ok = True
        head, _, tail = out.partition("__FRLOG__")
        p.factory_log = tail.strip()
        for line in head.splitlines():
            line = line.strip()
            if line.startswith("BIN="):
                p.binary_present = line.endswith("1")
            elif line.startswith("CFG="):
                p.config_present = line.endswith("1")
            elif line.startswith("GUARD="):
                p.guard_running = line.endswith("1")
            elif line.startswith("VAL="):
                p.valetudo_running = line.endswith("1")
            elif line.startswith("UP="):
                try:
                    p.uptime_s = int(line[3:])
                except ValueError:
                    pass
            elif line.startswith("CKP="):
                try:
                    p.wipes_prevented = int(line[4:] or 0)
                except ValueError:
                    pass
            elif line.startswith("CKG="):
                p.guard_standing_down = line.endswith("1")
            elif line.startswith("CKL="):
                p.last_strike = line[4:]
        return p

    def task_state(self) -> dict:
        """
        Whether the robot is free to have its map swapped.

        `idle` is the firmware's own test, the one its nightly reboot job
        (check_restart_ava.sh) asks before it reboots. `status` is Valetudo's
        robot status ("docked", "cleaning", ...). The Valetudo login is read
        from the robot's own config ON the robot and used against localhost
        only, so it never leaves the robot. An unreadable status comes back as
        "", which callers must treat as "not confirmed docked".
        """
        script = (
            "echo __VR_OK__; "
            "echo IDLE=$(avacmd msg_cvt '{\"type\":\"msgCvt\", \"cmd\":\"status_idle\", "
            "\"rpt_task_state\":true}' 2>/dev/null); "
            "C=%s; "
            "U=$(tr -d '\\n' < $C | sed -n 's/.*\"basicAuth\"[^}]*\"username\": *\"\\([^\"]*\\)\".*/\\1/p'); "
            "P=$(tr -d '\\n' < $C | sed -n 's/.*\"basicAuth\"[^}]*\"password\": *\"\\([^\"]*\\)\".*/\\1/p'); "
            "echo STATUS=$(curl -s -m 8 -u \"$U:$P\" http://127.0.0.1/api/v2/robot/state/attributes "
            "| tr '{' '\\n' | grep -A1 '\"StatusStateAttribute' | tail -n 1 "
            "| sed -n 's/.*\"value\":\"\\([a-z_]*\\)\".*/\\1/p')"
        ) % _q(P_CONFIG)
        rc, out, err = self.run(script, timeout=self.timeout + 20)
        if "__VR_OK__" not in out:
            raise RobotUnreachable((err or out or "no sentinel in response").strip()[:400])
        state = {"idle": False, "status": ""}
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("IDLE="):
                state["idle"] = '"ok"' in line
            elif line.startswith("STATUS="):
                state["status"] = line[7:]
        return state

    def carpet_summary(self) -> dict:
        """
        Detected carpet in the robot's current map: outlines in
        carpet_map_large.json and flagged cells in carpet_path_large.txt,
        summed over /data/DivideMap. Cheap, so it can decide whether a strip
        is worth a backup and a map swap at all.
        """
        script = (
            "echo __VR_OK__; for d in /data/DivideMap/*/; do "
            "[ -f \"$d/carpet_map_large.json\" ] && "
            "echo O=$(grep -c '\"id\"' \"$d/carpet_map_large.json\"); "
            "[ -f \"$d/carpet_path_large.txt\" ] && "
            "echo C=$(awk 'NR>1 && $3!=0' \"$d/carpet_path_large.txt\" | wc -l); "
            "done"
        )
        rc, out, err = self.run(script, timeout=self.timeout + 20)
        if "__VR_OK__" not in out:
            raise RobotUnreachable((err or out or "no sentinel in response").strip()[:400])
        found = {"outlines": 0, "cells": 0}
        for line in out.splitlines():
            line = line.strip()
            try:
                if line.startswith("O="):
                    found["outlines"] += int(line[2:] or 0)
                elif line.startswith("C="):
                    found["cells"] += int(line[2:] or 0)
            except ValueError:
                pass
        return found

    def device_token(self) -> bytes:
        """The 16-byte miIO token, for app/miio.py. Do not log the result."""
        token = self.read_file(P_MIIO_TOKEN).strip()[:16]
        if len(token) != 16:
            raise FileNotFoundError("%s does not hold a 16-byte token" % P_MIIO_TOKEN)
        return token

    def carpet_zones(self) -> dict:
        """
        The carpet edits stored with each saved map: "ignore carpet" rectangles
        (nocpt) and hand-drawn carpets (addcpt), keyed by map file. Read from
        /data/ri/<slot>.dat2, which is where ava keeps them.
        """
        import json
        zones = {}
        rc, out, _ = self.run("ls /data/ri/*.dat2 2>/dev/null")
        for path in out.split():
            try:
                raw = self.read_file(path).decode("utf-8", "replace")
                d = json.loads(raw[raw.index("{"):])
            except Exception:
                continue
            zones[path.rsplit("/", 1)[-1]] = {
                "nocpt": d.get("virtual_wall_nocpt_rects_reuseinfo"),
                "addcpt": d.get("virtual_wall_addcpt_rects_reuseinfo"),
            }
        return zones

    def valetudo_api(self, method: str, path: str, body: Optional[str] = None) -> str:
        """
        Call Valetudo's own API from the robot itself (localhost). The login
        is read from the robot's config on the robot, so it never leaves it.
        Returns the response body; "" if Valetudo did not answer.
        """
        script = (
            "C=%s; "
            "U=$(tr -d '\\n' < $C | sed -n 's/.*\"basicAuth\"[^}]*\"username\": *\"\\([^\"]*\\)\".*/\\1/p'); "
            "P=$(tr -d '\\n' < $C | sed -n 's/.*\"basicAuth\"[^}]*\"password\": *\"\\([^\"]*\\)\".*/\\1/p'); "
            "curl -s -m 10 -u \"$U:$P\" -X %s -H 'Content-Type: application/json' %s"
            "http://127.0.0.1%s"
        ) % (_q(P_CONFIG), method, ("-d %s " % _q(body)) if body is not None else "", path)
        return self.run(script, timeout=self.timeout + 20)[1]

    def user_modes(self) -> dict:
        """
        The two settings the firmware has been seen to reset around restores:
        the cleaning mode ("vacuum", "vacuum_and_mop", ...) and Carpet Mode.
        A value is None when Valetudo could not report it.
        """
        import json
        modes = {"operation_mode": None, "carpet_mode": None}
        try:
            for a in json.loads(self.valetudo_api("GET", "/api/v2/robot/state/attributes")):
                if a.get("__class") == "PresetSelectionStateAttribute" and a.get("type") == "operation_mode":
                    modes["operation_mode"] = a.get("value")
        except Exception:
            pass
        try:
            v = json.loads(self.valetudo_api(
                "GET", "/api/v2/robot/capabilities/CarpetModeControlCapability")).get("enabled")
            if isinstance(v, bool):
                modes["carpet_mode"] = v
        except Exception:
            pass
        return modes

    def set_user_modes(self, operation_mode: Optional[str] = None,
                       carpet_mode: Optional[bool] = None) -> None:
        import json
        if operation_mode is not None:
            self.valetudo_api("PUT", "/api/v2/robot/capabilities/OperationModeControlCapability/preset",
                              json.dumps({"name": operation_mode}))
        if carpet_mode is not None:
            self.valetudo_api("PUT", "/api/v2/robot/capabilities/CarpetModeControlCapability",
                              json.dumps({"action": "enable" if carpet_mode else "disable"}))

    # ---------- restore helpers ----------
    def rebuild_boot_hook(self, include_keeper: bool = True,
                          include_crash_keeper: bool = False) -> str:
        """
        Rebuild /data/_root_postboot.sh from the dustbuilder template.

        The template lives on the read-only rootfs at /misc, so it survives a
        wipe and is always the correct base. Hand-rolling a minimal
        `/data/valetudo &` hook silently loses what the template does: disables
        wifi power management, pins the timezone to UTC, clears the dmiot flag
        and - critically - sets VALETUDO_CONFIG_PATH. Without that env var
        Valetudo writes its config to /tmp, which is tmpfs, so every setting is
        lost at the next reboot.
        """
        if not self.path_exists(P_POSTBOOT_TPL):
            raise FileNotFoundError(P_POSTBOOT_TPL + " missing on rootfs")
        self.run("cp %s %s" % (P_POSTBOOT_TPL, P_POSTBOOT))
        blocks = []
        if include_keeper:
            blocks.append(("wifi-keeper", P_GUARD))
        if include_crash_keeper:
            blocks.append(("crash-keeper", P_CRASH_KEEPER))
        if blocks:
            text = "".join(
                "\n# %s (valetudo-restore)\nif [ -x %s ]; then\n"
                "        %s > /dev/null 2>&1 &\nfi\n" % (label, path, path)
                for label, path in blocks)
            cur = self.read_file(P_POSTBOOT)
            self.write_file(P_POSTBOOT, cur + text.encode(), mode="0755")
        else:
            self.run("chmod 0755 " + P_POSTBOOT)
        return self.read_file(P_POSTBOOT).decode("utf-8", "replace")

    # ---------- carpet detection overrides ----------
    def laser_override_state(self) -> dict:
        """Is the laser override mounted right now, and is the block in the boot hook?"""
        rc, out, _ = self.run(
            "echo MOUNTED=$(grep -c '\"carpet_inspect\": false' %s 2>/dev/null); "
            "echo HOOK=$(grep -c 'carpet-overrides (valetudo-restore)' %s 2>/dev/null)"
            % (LASER_CONF, P_POSTBOOT))
        n = {}
        for line in out.splitlines():
            if "=" in line:
                k, _, v = line.strip().partition("=")
                try:
                    n[k] = int(v or 0)
                except ValueError:
                    n[k] = 0
        return {"mounted": n.get("MOUNTED", 0) > 0, "hook": n.get("HOOK", 0) > 0}

    def install_laser_override(self) -> bool:
        """Put the override block in the boot hook. True if the hook changed."""
        cur = self.read_file(P_POSTBOOT).decode("utf-8", "replace")
        new = hook_with_laser_override(cur)
        if new == cur:
            return False
        self.write_file(P_POSTBOOT, new.encode("utf-8"), mode="0755")
        return True

    def mount_laser_override(self) -> str:
        """
        Mount the override now. Only takes effect for an ava that starts afterwards, so
        callers use it while ava is stopped. Returns "mounted", "already" or "failed".
        """
        rc, out, _ = self.run(
            "if grep -q '\"carpet_inspect\": false' %s; then echo already; "
            "elif %s && grep -q '\"carpet_inspect\": false' %s "
            "&& mount --bind %s %s; then echo mounted; else echo failed; fi"
            % (LASER_CONF, _LASER_SED, LASER_CONF_COPY, LASER_CONF_COPY, LASER_CONF))
        word = out.strip().splitlines()[-1] if out.strip() else "failed"
        return word if word in ("mounted", "already") else "failed"

    def apply_laser_override_live(self) -> str:
        """Stop ava, mount the override, start ava again. Returns the mount result."""
        self.stop_map_processes()
        try:
            return self.mount_laser_override()
        finally:
            self.start_map_processes()

    def start_valetudo(self) -> None:
        # setsid: a plain `&` over an ssh exec channel can die with the session.
        self.run(
            "VALETUDO_CONFIG_PATH=%s setsid %s >/dev/null 2>&1 </dev/null &"
            % (P_CONFIG, P_VALETUDO)
        )

    def start_script(self, path: str, pidfile: str) -> None:
        """
        (Re)start a helper daemon. Any running copy is stopped first, so an
        older version or a stale pidfile can never leave two running.
        """
        name = path.rsplit("/", 1)[-1]
        self.run("kill $(ps | grep -v grep | grep %s | awk '{print $1}') 2>/dev/null; "
                 "rm -f %s" % (name, pidfile))
        self.run("[ -x %s ] && setsid %s >/dev/null 2>&1 </dev/null &" % (path, path))

    def start_guard(self) -> None:
        self.run(
            "[ -x %s ] && setsid %s >/dev/null 2>&1 </dev/null &" % (P_GUARD, P_GUARD)
        )

    def stop_valetudo(self, timeout: int = 15) -> bool:
        """SIGTERM Valetudo and wait for it to actually exit."""
        rc, out, _ = self.run("pidof valetudo")
        if rc != 0 or not out.strip():
            return True  # already stopped
        self.run("kill %s" % out.strip())
        for _ in range(timeout):
            rc, out, _ = self.run("pidof valetudo")
            if rc != 0 or not out.strip():
                return True
            self.run("sleep 1")
        # last resort
        self.run("killall -9 valetudo")
        rc, out, _ = self.run("pidof valetudo")
        return rc != 0 or not out.strip()

    def stop_map_processes(self) -> None:
        """
        Stop ava and miio_client so map files can be swapped underneath them.

        maploader does this rather than rebooting: with ava running it holds the
        map open and will rewrite or discard whatever you put there.

        SIGTERM first, and only SIGKILL if it will not go. maploader uses
        `killall -9` outright, but ava holds vendor config and map state in
        memory and SIGKILL gives it no chance to flush - doing that repeatedly
        is a plausible way to corrupt exactly the state whose corruption
        triggers a factory reset. Not worth the risk for the sake of a few
        seconds.
        """
        self.run("sh /etc/rc.d/miio.sh stop", timeout=60)
        self.run("killall ava", timeout=60)
        for _ in range(10):
            if not self.run("pidof ava")[1].strip():
                return
            self.run("sleep 1")
        log.warning("ava did not exit on SIGTERM; escalating to SIGKILL")
        self.run("killall -9 ava", timeout=30)

    def start_map_processes(self) -> None:
        self.run("sh /etc/rc.d/ava.sh >/dev/null 2>&1 &", timeout=60)
        self.run("sh /etc/rc.d/miio.sh >/dev/null 2>&1 &", timeout=60)

    def reboot(self) -> None:
        """
        Reboot the robot.

        `sync` first, and detach the reboot so the ssh channel closing does not
        race the shutdown.
        """
        self.run("sync; (sleep 1; reboot) >/dev/null 2>&1 &")
