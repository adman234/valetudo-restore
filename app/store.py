"""Event log, monitor state and backup inventory (SQLite)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .models import CONFIG_DIR

DB_PATH = CONFIG_DIR / "state.db"
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    level     TEXT    NOT NULL,
    kind      TEXT    NOT NULL,
    message   TEXT    NOT NULL,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts DESC);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backups (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    filename  TEXT    NOT NULL UNIQUE,
    size      INTEGER NOT NULL,
    kind      TEXT    NOT NULL,
    ok        INTEGER NOT NULL DEFAULT 1,
    note      TEXT
);
CREATE INDEX IF NOT EXISTS ix_backups_ts ON backups(ts DESC);
"""


def _conn() -> sqlite3.Connection:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _lock, _conn() as c:
        c.executescript(SCHEMA)


# ---------- events ----------
def log_event(level: str, kind: str, message: str, detail: Any = None) -> None:
    det = None
    if detail is not None:
        det = detail if isinstance(detail, str) else json.dumps(detail, default=str)
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO events (ts, level, kind, message, detail) VALUES (?,?,?,?,?)",
            (int(time.time()), level, kind, message, det),
        )
        # keep the log bounded
        c.execute(
            "DELETE FROM events WHERE id NOT IN "
            "(SELECT id FROM events ORDER BY ts DESC LIMIT 2000)"
        )


def recent_events(limit: int = 100) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- key/value ----------
def kv_set(k: str, v: Any) -> None:
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO kv (k, v) VALUES (?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, json.dumps(v, default=str)),
        )


def kv_get(k: str, default: Any = None) -> Any:
    with _lock, _conn() as c:
        row = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["v"])
    except Exception:
        return default


# ---------- backups ----------
def add_backup(filename: str, size: int, kind: str, ok: bool = True,
               note: Optional[str] = None) -> None:
    with _lock, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO backups (ts, filename, size, kind, ok, note) "
            "VALUES (?,?,?,?,?,?)",
            (int(time.time()), filename, size, kind, 1 if ok else 0, note),
        )


def list_backups() -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute("SELECT * FROM backups ORDER BY ts DESC").fetchall()
    return [dict(r) for r in rows]


def forget_backup(filename: str) -> None:
    with _lock, _conn() as c:
        c.execute("DELETE FROM backups WHERE filename=?", (filename,))


def reconcile_backups(backup_dir: Path) -> None:
    """Drop DB rows whose files vanished; adopt files the DB does not know."""
    known = {b["filename"] for b in list_backups()}
    on_disk = {p.name for p in backup_dir.glob("*.tar.gz")} if backup_dir.exists() else set()
    for missing in known - on_disk:
        forget_backup(missing)
    for extra in on_disk - known:
        p = backup_dir / extra
        add_backup(extra, p.stat().st_size, "adopted", True, "found on disk")
