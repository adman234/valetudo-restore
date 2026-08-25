"""Configuration model and persistence."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

CONFIG_DIR = Path(os.environ.get("VR_CONFIG_DIR", "./config"))
BACKUP_DIR = Path(os.environ.get("VR_BACKUP_DIR", "./backups"))
SETTINGS_FILE = CONFIG_DIR / "settings.json"

_lock = threading.Lock()


class Settings(BaseModel):
    # --- robot connection ---
    robot_host: str = "192.168.50.117"
    robot_port: int = 22
    robot_user: str = "root"
    ssh_key_path: str = "/config/valetudo_key"
    ssh_timeout: int = 10

    # --- backup ---
    backup_enabled: bool = True
    backup_cron_hour: int = 2
    backup_cron_minute: int = 30
    keep_backups: int = Field(14, ge=1, le=365)

    # --- monitoring ---
    monitor_enabled: bool = True
    poll_interval_minutes: int = Field(5, ge=1, le=120)
    # A single sample is noisy (wifi roaming, robot rebooting). Require this
    # many consecutive identical verdicts before acting or alerting.
    confirm_samples: int = Field(2, ge=1, le=10)

    # --- recovery ---
    # Off by default: restoring writes to the robot, so it is opt-in.
    auto_restore: bool = False
    max_restore_attempts: int = Field(3, ge=1, le=10)
    restore_window_hours: int = Field(6, ge=1, le=168)
    # Reinstall the wipe-guard + boot hook alongside Valetudo itself.
    restore_wipe_guard: bool = True

    # --- notifications ---
    notify_on_wipe: bool = True
    notify_on_crash: bool = True
    notify_on_restore: bool = True
    notify_on_backup_failure: bool = True
    webhook_url: str = ""
    webhook_headers: str = ""  # JSON object, optional

    # --- valetudo binary ---
    valetudo_arch: Literal["aarch64", "armv7", "amd64"] = "aarch64"
    auto_download_binary: bool = True

    def cron_summary(self) -> str:
        return f"{self.backup_cron_hour:02d}:{self.backup_cron_minute:02d}"


def load_settings() -> Settings:
    with _lock:
        if SETTINGS_FILE.exists():
            try:
                return Settings(**json.loads(SETTINGS_FILE.read_text("utf-8")))
            except Exception:
                # Never let a corrupt settings file stop the app booting; fall
                # back to defaults and keep the bad file for inspection.
                bad = SETTINGS_FILE.with_suffix(".json.bad")
                try:
                    SETTINGS_FILE.replace(bad)
                except OSError:
                    pass
        return Settings()


def save_settings(s: Settings) -> None:
    with _lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(s.model_dump(), indent=2), "utf-8")
        tmp.replace(SETTINGS_FILE)
