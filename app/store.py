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

# Columns added after the first release. CREATE TABLE IF NOT EXISTS never alters
# an existing table, so each one is added here when it is missing.
MIGRATIONS = {
    "backups": [
        # 1 = full, 0 = incomplete, NULL = not assessed yet
        ("complete", "INTEGER"),
        # JSON list of human-readable reasons behind an incomplete verdict
        ("reasons", "TEXT"),
        # the user vouched for this backup, overriding the automatic verdict
        ("override", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def _conn() -> sqlite3.Connection:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _lock, _conn() as c:
        c.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in c.execute("PRAGMA table_info(%s)" % table)}
            for name, ddl in cols:
                if name not in have:
                    c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl))


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
            "(SELECT id FROM events ORDER BY ts DESC, id DESC LIMIT 2000)"
        )


def recent_events(limit: int = 100) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM events ORDER BY ts DESC, id DESC LIMIT ?", (limit,)
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
               note: Optional[str] = None, ts: Optional[int] = None) -> None:
    """Record a backup. Its completeness is assessed separately, afterwards."""
    with _lock, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO backups (ts, filename, size, kind, ok, note) "
            "VALUES (?,?,?,?,?,?)",
            (int(time.time()) if ts is None else int(ts), filename, size, kind,
             1 if ok else 0, note),
        )


def list_backups() -> list[dict]:
    """
    Newest first. Each row carries `full`: the ONE definition of whether a
    backup can be trusted to put the robot back. The user's override wins over
    the automatic verdict, and an unassessed backup is not full.
    """
    with _lock, _conn() as c:
        # ts has one-second resolution; id breaks ties, so "newest" is always
        # well defined, and so is the order of events logged in the same second.
        rows = c.execute("SELECT * FROM backups ORDER BY ts DESC, id DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["reasons"] = json.loads(d.get("reasons") or "[]")
        except ValueError:
            d["reasons"] = []
        d["full"] = bool(d.get("override")) or d.get("complete") == 1
        out.append(d)
    return out


def set_assessment(filename: str, complete: bool, reasons: list) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE backups SET complete=?, reasons=? WHERE filename=?",
                  (1 if complete else 0, json.dumps(reasons), filename))


def set_override(filename: str, on: bool) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE backups SET override=? WHERE filename=?",
                  (1 if on else 0, filename))


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
        # Date it by the file, not by when it was found. Stamping adopted files
        # with "now" made an old archive look like the newest backup, which is
        # exactly the one a restore picks.
        st = p.stat()
        add_backup(extra, st.st_size, "adopted", True, "found on disk",
                   ts=int(st.st_mtime))
