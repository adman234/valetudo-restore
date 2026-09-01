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
    # Voice packs are a few MB of user-installed audio that a wipe destroys and
    # that cannot be regenerated without the original download URL.
    backup_voice_pack: bool = True

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
    # Reinstall the wifi-keeper + boot hook alongside Valetudo itself.
    # The boot hook is NOT optional in practice: /etc/rc.sysinit:73 is the only
    # thing that starts Valetudo, and it carries VALETUDO_CONFIG_PATH.
    restore_wifi_keeper: bool = True

    # --- notifications ---
    notify_on_wipe: bool = True
    notify_on_crash: bool = True
    notify_on_restore: bool = True
    notify_on_backup_failure: bool = True
    webhook_url: str = ""
    webhook_headers: str = ""  # JSON object, optional

    # --- restore extras ---
    # Vendor settings (obstacle images, pet avoidance, carpet handling, mop
    # options, room names) live in /data/config/ava. Restoring them is opt-out
    # because a wipe is usually triggered by something in /data being wrong.
    restore_vendor_settings: bool = True
    restore_duststreamer: bool = True
    # Neither of these is recoverable from the robot: Valetudo takes a URL and a
    # hash to install a voice pack and persists neither. Record them here so a
    # rebuild does not depend on remembering where they came from. The pack
    # files themselves ARE backed up; these are the fallback.
    voice_pack_url: str = ""
    voice_pack_hash: str = ""
    # Only an aarch64 build is published, so this is not templated on arch.
    duststreamer_url: str = (
        "https://github.com/Hypfer/Duststreamer/releases/latest/download/"
        "duststreamer-aarch64")

    # --- valetudo binary ---
    valetudo_arch: Literal["aarch64", "armv7", "amd64"] = "aarch64"
    auto_download_binary: bool = True

    def cron_summary(self) -> str:
        return f"{self.backup_cron_hour:02d}:{self.backup_cron_minute:02d}"


# Environment variable -> settings field. These SEED the initial configuration
# on first run only; once settings.json exists the web UI is authoritative, so
# editing a setting in the UI is never silently undone by a stale env var.
ENV_MAP = {
    "VR_ROBOT_HOST": "robot_host",
    "VR_ROBOT_PORT": "robot_port",
    "VR_ROBOT_USER": "robot_user",
    "VR_SSH_KEY_PATH": "ssh_key_path",
    "VR_SSH_TIMEOUT": "ssh_timeout",
    "VR_BACKUP_ENABLED": "backup_enabled",
    "VR_BACKUP_HOUR": "backup_cron_hour",
    "VR_BACKUP_MINUTE": "backup_cron_minute",
    "VR_KEEP_BACKUPS": "keep_backups",
    "VR_BACKUP_VOICE_PACK": "backup_voice_pack",
    "VR_MONITOR_ENABLED": "monitor_enabled",
    "VR_POLL_INTERVAL_MINUTES": "poll_interval_minutes",
    "VR_CONFIRM_SAMPLES": "confirm_samples",
    "VR_AUTO_RESTORE": "auto_restore",
    "VR_MAX_RESTORE_ATTEMPTS": "max_restore_attempts",
    "VR_RESTORE_WINDOW_HOURS": "restore_window_hours",
    "VR_RESTORE_WIFI_KEEPER": "restore_wifi_keeper",
    "VR_WEBHOOK_URL": "webhook_url",
    "VR_WEBHOOK_HEADERS": "webhook_headers",
    "VR_NOTIFY_ON_WIPE": "notify_on_wipe",
    "VR_NOTIFY_ON_CRASH": "notify_on_crash",
    "VR_NOTIFY_ON_RESTORE": "notify_on_restore",
    "VR_NOTIFY_ON_BACKUP_FAILURE": "notify_on_backup_failure",
    "VR_RESTORE_VENDOR_SETTINGS": "restore_vendor_settings",
    "VR_RESTORE_DUSTSTREAMER": "restore_duststreamer",
    "VR_VOICE_PACK_URL": "voice_pack_url",
    "VR_VOICE_PACK_HASH": "voice_pack_hash",
    "VR_DUSTSTREAMER_URL": "duststreamer_url",
    "VR_VALETUDO_ARCH": "valetudo_arch",
    "VR_AUTO_DOWNLOAD_BINARY": "auto_download_binary",
}

_TRUE = ("1", "true", "yes", "on")


def settings_from_env() -> dict:
    """Read VR_* environment variables into a settings dict."""
    defaults = Settings().model_dump()
    out: dict = {}
    for env_key, field in ENV_MAP.items():
        raw = os.environ.get(env_key)
        if raw is None or raw == "":
            continue
        default = defaults[field]
        if isinstance(default, bool):
            out[field] = raw.strip().lower() in _TRUE
        elif isinstance(default, int):
            try:
                out[field] = int(raw)
            except ValueError:
                continue
        else:
            out[field] = raw
    return out


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
        # First run: seed from the environment.
        try:
            return Settings(**settings_from_env())
        except Exception:
            return Settings()


def save_settings(s: Settings) -> None:
    with _lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(s.model_dump(), indent=2), "utf-8")
        tmp.replace(SETTINGS_FILE)
