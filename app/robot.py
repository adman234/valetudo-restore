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
P_GUARD = "/data/_wipe_guard.sh"
P_POSTBOOT = "/data/_root_postboot.sh"
P_POSTBOOT_TPL = "/misc/_root_postboot.sh.tpl"
P_FACTORY_LOG = "/data/log/factory_reset.log"

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
DUSTSTREAMER_ITEM = ("/data/duststreamer", "duststreamer", False)

# What a backup captures: (remote_path, archive_member_name, is_dir)
BACKUP_ITEMS = [
    ("/data/valetudo_config.json", "valetudo_config.json", False),
    ("/data/_wipe_guard.sh", "_wipe_guard.sh", False),
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
    ("/data/DivideAI", "data_divideai.tar.gz", True),                # AI obstacle data
] + MAP_PATHS + [DUSTSTREAMER_ITEM]

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
            "ps | grep -v grep | grep -q _wipe_guard.sh && echo GUARD=1 || echo GUARD=0; "
            "pidof valetudo >/dev/null && echo VAL=1 || echo VAL=0; "
            "echo UP=$(cut -d. -f1 /proc/uptime); "
            "echo __FRLOG__; cat %s 2>/dev/null"
        ) % (P_VALETUDO, P_CONFIG, P_FACTORY_LOG)

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
        return p

    # ---------- restore helpers ----------
    def rebuild_boot_hook(self, include_guard: bool = True) -> str:
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
        if include_guard:
            block = (
                "\n# wipe-guard (valetudo-restore)\n"
                "if [ -x " + P_GUARD + " ]; then\n"
                "        " + P_GUARD + " > /dev/null 2>&1 &\n"
                "fi\n"
            )
            cur = self.read_file(P_POSTBOOT)
            self.write_file(P_POSTBOOT, cur + block.encode(), mode="0755")
        else:
            self.run("chmod 0755 " + P_POSTBOOT)
        return self.read_file(P_POSTBOOT).decode("utf-8", "replace")

    def start_valetudo(self) -> None:
        # setsid: a plain `&` over an ssh exec channel can die with the session.
        self.run(
            "VALETUDO_CONFIG_PATH=%s setsid %s >/dev/null 2>&1 </dev/null &"
            % (P_CONFIG, P_VALETUDO)
        )

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

        maploader does exactly this rather than rebooting: with ava running, it
        holds the map open and will rewrite or discard whatever you put there.
        """
        self.run("sh /etc/rc.d/miio.sh stop", timeout=60)
        self.run("killall -9 ava", timeout=60)

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
