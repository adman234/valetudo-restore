"""
A minimal miIO client: enough to send one command to the robot over the LAN
and read the answer.

Valetudo has controls for most things but no way to pass an arbitrary command
through, and a few firmware features have no Valetudo control at all (carpet
edits, for one). The robot's `miio_client` listens on UDP 54321 and accepts the
same JSON commands Valetudo sends it, encrypted with the device token.

The protocol, as implemented by every open miIO library:

    packet  = header (32 bytes) + AES-128-CBC(payload)
    header  = 0x2131 | length (2) | 0 (4) | device id (4) | stamp (4) | md5 (16)
    key     = md5(token)            iv = md5(key + token)
    md5     = md5(header[:16] + token + encrypted payload)

A "hello" (0x2131, length 32, the rest 0xff) is answered with the device id and
the device's current stamp, which every real packet must carry.

The token is the robot's own secret. Callers read it from the robot when they
need it and hand it in; nothing here stores or logs it.
"""
from __future__ import annotations

import hashlib
import json
import socket
import struct
import time
from typing import Any, Optional

# cryptography is already installed as a dependency of paramiko.
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PORT = 54321
HELLO = bytes.fromhex("21310020" + "ff" * 28)


class MiioError(Exception):
    """The robot did not answer, or answered with something unusable."""


def _md5(b: bytes) -> bytes:
    return hashlib.md5(b).digest()


def _keys(token: bytes) -> tuple[bytes, bytes]:
    key = _md5(token)
    return key, _md5(key + token)


def encrypt(token: bytes, plain: bytes) -> bytes:
    key, iv = _keys(token)
    padder = padding.PKCS7(128).padder()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(padder.update(plain) + padder.finalize()) + enc.finalize()


def decrypt(token: bytes, data: bytes) -> bytes:
    key, iv = _keys(token)
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(dec.update(data) + dec.finalize()) + unpadder.finalize()


def build_packet(token: bytes, device_id: int, stamp: int, payload: dict) -> bytes:
    body = encrypt(token, json.dumps(payload, separators=(",", ":")).encode() + b"\x00")
    head = struct.pack(">HHIII", 0x2131, 32 + len(body), 0, device_id, stamp)
    return head + _md5(head + token + body) + body


def parse_packet(token: bytes, packet: bytes) -> dict:
    if len(packet) < 32 or packet[:2] != b"\x21\x31":
        raise MiioError("not a miIO packet")
    length = struct.unpack(">H", packet[2:4])[0]
    body = packet[32:length]
    if not body:
        raise MiioError("empty answer")
    if _md5(packet[:16] + token + body) != packet[16:32]:
        raise MiioError("checksum mismatch: wrong token, or not this robot's answer")
    try:
        text = decrypt(token, body).rstrip(b"\x00").decode("utf-8", "replace")
        return json.loads(text)
    except Exception as e:
        raise MiioError("could not read the answer: %s" % e)


class MiioClient:
    def __init__(self, host: str, token: bytes, port: int = PORT, timeout: float = 5.0):
        if len(token) != 16:
            raise MiioError("the device token must be 16 bytes")
        self.host, self.port, self.timeout = host, port, timeout
        self._token = token
        self.device_id: Optional[int] = None
        self._stamp = 0
        self._stamp_at = 0.0
        self._id = int(time.time()) % 100000

    def __repr__(self) -> str:          # never show the token
        return "MiioClient(%s:%d)" % (self.host, self.port)

    def _exchange(self, data: bytes, retries: int = 2) -> bytes:
        last = None
        for _ in range(retries + 1):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(self.timeout)
                try:
                    sock.sendto(data, (self.host, self.port))
                    return sock.recvfrom(65535)[0]
                except OSError as e:
                    last = e
        raise MiioError("no answer from %s:%d (%s)" % (self.host, self.port, last))

    def hello(self) -> int:
        """Learn the device id and its clock. Returns the device id."""
        ans = self._exchange(HELLO)
        if len(ans) < 32 or ans[:2] != b"\x21\x31":
            raise MiioError("unexpected answer to hello")
        _, _, _, self.device_id, self._stamp = struct.unpack(">HHIII", ans[:16])
        self._stamp_at = time.time()
        return self.device_id

    def send(self, method: str, params: Any) -> dict:
        """Send one command and return the robot's JSON answer."""
        if self.device_id is None:
            self.hello()
        self._id += 1
        stamp = self._stamp + int(time.time() - self._stamp_at) + 1
        packet = build_packet(self._token, self.device_id, stamp,
                              {"id": self._id, "method": method, "params": params})
        ans = parse_packet(self._token, self._exchange(packet))
        if "error" in ans:
            raise MiioError("the robot refused %s: %s" % (method, ans["error"]))
        return ans

    # ---- MIoT, which is what Dreame firmware speaks --------------------------
    def get_properties(self, props: list[tuple[int, int]]) -> dict:
        """{(siid, piid): value}; a property the robot does not have is None."""
        did = str(self.device_id or self.hello())
        res = self.send("get_properties",
                        [{"did": did, "siid": s, "piid": p} for s, p in props])
        out = {}
        for r in res.get("result") or []:
            out[(r.get("siid"), r.get("piid"))] = r.get("value") if r.get("code") == 0 else None
        return out

    def action(self, siid: int, aiid: int, params: list[dict]) -> dict:
        did = str(self.device_id or self.hello())
        return self.send("action", {"did": did, "siid": siid, "aiid": aiid, "in": params})
