"""
Local miio (MIoT) transport for Dreame/Mova robots.

Why this exists
---------------
Valetudo owns the robot's cloud-facing side, but the vendor's `miio_client` is
still listening on UDP 54321 on all interfaces, and Valetudo does not occupy
that port. That gives us a local, token-authenticated RPC channel to the vendor
firmware while Valetudo carries on as normal.

This is the channel the Dreame HA integration uses for offline map recovery, and
it is the only one that works: the `avacmd msg_cvt` route looks promising but its
`get_prop` returns `{"value":["unknow"],"ret":"ok"}` for *any* name -- including
nonsense -- so it exposes none of these properties.

Verified against a real r2416 (mova.vacuum.r2491a, fw 4.3.9_1782):

    miIO.info                        -> full device info
    get_properties siid=6 piid=9     -> {"object_name": "..."}   recovery map list
    get_properties siid=6 piid=11    -> 0                        recovery status
    set_properties siid=6 piid=10    -> code 0, robot fetched our URL with wget

Map recovery properties (siid 6):

    piid  9   RECOVERY_MAP_LIST     read
    piid 10   MAP_RECOVERY          write-only (reads return code -1)
    piid 11   MAP_RECOVERY_STATUS   0 idle / 1 running / 2 success / 3 fail
    piid 14   MAP_BACKUP_STATUS

The value written to 6/10 is a JSON *string*. `map_id` is mandatory when there is
no cloud connection -- without it the write is rejected with code -1, which is
exactly what the integration's docs mean by "Map ID is required if cloud
connection is not enabled".
"""
from __future__ import annotations

import hashlib
import json
import logging
import socket
import struct
import time
from typing import Any, Optional

from Crypto.Cipher import AES

log = logging.getLogger("vr.miio")

NUL = bytes([0])   # constant, not an inline escape (which tooling keeps mangling)
MIIO_PORT = 54321
HELLO = bytes.fromhex("21310020" + "ff" * 28)

# siid 6 - map service
SIID_MAP = 6
PIID_RECOVERY_MAP_LIST = 9
PIID_MAP_RECOVERY = 10
PIID_MAP_RECOVERY_STATUS = 11
PIID_MAP_BACKUP_STATUS = 14

# Status codes. 0/1 observed directly (idle -> running on a real trigger) and 5
# observed as the terminal state after deliberately serving an invalid file, so
# 5 is a failure. 2/3/4 follow the integration's enum
# (UNKNOWN/IDLE/RUNNING/SUCCESS/FAIL/FAIL_2) and are NOT yet confirmed on this
# firmware - treat anything that is not 0 or 1 as terminal and check the value.
RECOVERY_STATUS = {
    0: "idle",
    1: "running",
    2: "success",
    3: "fail",
    4: "fail",
    5: "fail",
}
TERMINAL_STATUS = (0, 2, 3, 4, 5)


class MiioError(Exception):
    pass


def _keys(token: bytes):
    key = hashlib.md5(token).digest()
    return key, hashlib.md5(key + token).digest()


def _encrypt(token: bytes, plain: bytes) -> bytes:
    key, iv = _keys(token)
    pad = 16 - len(plain) % 16
    return AES.new(key, AES.MODE_CBC, iv).encrypt(plain + bytes([pad]) * pad)


def _decrypt(token: bytes, data: bytes) -> bytes:
    key, iv = _keys(token)
    out = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
    return out[: -out[-1]] if out else out


def normalise_token(raw: str) -> bytes:
    """Accept the token as 16 raw ASCII chars or 32 hex chars."""
    t = raw.strip()
    if len(t) == 32:
        try:
            return bytes.fromhex(t)
        except ValueError:
            pass
    if len(t) == 16:
        return t.encode()
    raise MiioError("token must be 16 ASCII chars or 32 hex chars, got %d" % len(t))


class MiioClient:
    """Minimal local miio client. One socket per instance; not thread-safe."""

    def __init__(self, host: str, token: str, port: int = MIIO_PORT,
                 timeout: float = 10.0, retries: int = 4):
        self.host = host
        self.port = port
        self.token = normalise_token(token)
        self.timeout = timeout
        # Retries cover genuine UDP loss (this robot's wifi roams between APs).
        # NOTE: retries alone do NOT fix a replayed request id - see _id below.
        # A stale id makes every attempt fail identically, which is what makes
        # it look like packet loss or a dead device.
        self.retries = retries
        self._sock: Optional[socket.socket] = None
        self._did: Optional[int] = None
        self._stamp: int = 0
        self._stamp_at: float = 0.0
        # The device tracks the last request id it saw and DROPS anything not
        # greater - across connections, not just within one. Starting from a
        # fixed number means the second client replays an id the robot has
        # already seen and every reply is silently withheld, which looks
        # exactly like a dead device. Seed from the clock so ids always climb,
        # even across container restarts.
        self._id = int(time.time()) % 2000000

    def __enter__(self) -> "MiioClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(self.timeout)
        try:
            s.sendto(HELLO, (self.host, self.port))
            data, _ = s.recvfrom(1024)
        except socket.timeout as e:
            s.close()
            raise MiioError(
                "no miio handshake from %s:%s - is miio_client running?"
                % (self.host, self.port)) from e
        if len(data) < 16:
            s.close()
            raise MiioError("short handshake reply (%d bytes)" % len(data))
        _, _, _, did, stamp = struct.unpack(">HHIII", data[:16])
        self._sock, self._did, self._stamp = s, did, stamp
        self._stamp_at = time.monotonic()
        log.debug("miio handshake ok did=%s stamp=%s", did, stamp)

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None

    @property
    def device_id(self) -> Optional[int]:
        return self._did

    def call(self, method: str, params: Any) -> dict:
        """
        Send an RPC, retrying on timeout.

        UDP loss on this robot's wifi is common enough that a single timeout
        means nothing. Each attempt re-sends with a freshly computed stamp; the
        device tolerates a repeated request id.
        """
        if not self._sock:
            raise MiioError("not connected")
        self._id += 1
        payload = json.dumps(
            {"id": self._id, "method": method, "params": params}).encode()
        body = _encrypt(self.token, payload)

        last = None
        for attempt in range(1, self.retries + 1):
            # The stamp must track the device's clock, NOT the message id.
            # handshake_stamp + id puts packets into the future and they are
            # silently dropped, which also looks like a timeout.
            stamp = self._stamp + int(time.monotonic() - self._stamp_at)
            header = struct.pack(">HHIII", 0x2131, 32 + len(body), 0,
                                 self._did, stamp)
            pkt = header + hashlib.md5(header + self.token + body).digest() + body
            try:
                self._sock.sendto(pkt, (self.host, self.port))
                resp, _ = self._sock.recvfrom(65536)
            except socket.timeout as e:
                last = e
                log.debug("miio %s attempt %d/%d timed out",
                          method, attempt, self.retries)
                continue
            try:
                return json.loads(
                    _decrypt(self.token, resp[32:]).rstrip(NUL).decode())
            except Exception as e:
                raise MiioError("could not decode reply to %s (bad token?): %s"
                                % (method, e)) from e
        raise MiioError("no reply to %s after %d attempts (udp loss?): %s"
                        % (method, self.retries, last))

    # ---------- convenience ----------
    def info(self) -> dict:
        return self.call("miIO.info", []).get("result", {})

    def get_props(self, *piids: int, siid: int = SIID_MAP) -> dict:
        r = self.call("get_properties",
                      [{"did": "vr", "siid": siid, "piid": p} for p in piids])
        out = {}
        for item in r.get("result", []):
            out[item["piid"]] = item.get("value", "code=%s" % item.get("code"))
        return out

    def set_prop(self, piid: int, value: Any, siid: int = SIID_MAP) -> int:
        r = self.call("set_properties",
                      [{"did": "vr", "siid": siid, "piid": piid, "value": value}])
        res = (r.get("result") or [{}])[0]
        return res.get("code", -1)

    # ---------- map recovery ----------
    def recovery_status(self) -> tuple[int, str]:
        v = self.get_props(PIID_MAP_RECOVERY_STATUS).get(PIID_MAP_RECOVERY_STATUS)
        code = v if isinstance(v, int) else -1
        return code, RECOVERY_STATUS.get(code, "unknown(%s)" % v)

    def recovery_map_list(self) -> Any:
        return self.get_props(PIID_RECOVERY_MAP_LIST).get(PIID_RECOVERY_MAP_LIST)

    def restore_map(self, map_url: str, map_id: int,
                    req_type: int = 1, force_type: int = 1) -> int:
        """
        Ask the robot to download a recovery map from `map_url` and restore it.

        Returns the MIoT result code: 0 accepted, -1 rejected. `map_id` is
        mandatory offline - omitting it is rejected outright. The robot fetches
        the URL itself (with wget), so it must be reachable *from the robot*.
        """
        value = json.dumps({
            "map_url": map_url,
            "map_id": map_id,
            "req_type": req_type,
            "force_type": force_type,
        })
        log.info("miio restore_map -> %s", value)
        return self.set_prop(PIID_MAP_RECOVERY, value)
