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

# Paths on the robot
P_VALETUDO = "/data/valetudo"
P_CONFIG = "/data/valetudo_config.json"
P_GUARD = "/data/_wipe_guard.sh"
P_POSTBOOT = "/data/_root_postboot.sh"
P_POSTBOOT_TPL = "/misc/_root_postboot.sh.tpl"
P_FACTORY_LOG = "/data/log/factory_reset.log"

# What a backup captures: (remote_path, archive_member_name, is_dir)
BACKUP_ITEMS = [
    ("/data/valetudo_config.json", "valetudo_config.json", False),
    ("/data/_wipe_guard.sh", "_wipe_guard.sh", False),
    ("/data/_root_postboot.sh", "_root_postboot.sh", False),
    ("/data/log/factory_reset.log", "factory_reset.log", False),
    ("/data/config", "data_config.tar.gz", True),
    ("/data/map", "data_map.tar.gz", True),
    ("/mnt/misc", "misc.tar.gz", True),
    ("/mnt/private", "private.tar.gz", True),
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
                   timeout: int = 900) -> None:
        """
        Stream bytes to a remote path.

        scp/sftp are unavailable on this robot, so this pipes through `cat >`.
        Shell scripts are normalised to LF, because BusyBox ash cannot parse
        CRLF and fails in a way that looks like the script simply never ran.
        """
        assert self._c, "not connected"
        if path.endswith(".sh") and b"\r\n" in data:
            log.warning("normalising CRLF -> LF for %s", path)
            data = data.replace(b"\r\n", b"\n")
        chan = self._c.get_transport().open_session(timeout=self.timeout)
        chan.settimeout(timeout)
        chan.exec_command("cat > " + _q(path))
        wfile = chan.makefile("wb")
        try:
            wfile.write(data)
            wfile.flush()
        finally:
            wfile.close()
        chan.shutdown_write()
        rc = chan.recv_exit_status()
        chan.close()
        if rc != 0:
            raise IOError("write %s failed (rc=%s)" % (path, rc))
        self.run("chmod %s %s" % (mode, _q(path)))

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
