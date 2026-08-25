# valetudo-restore

Backup, monitoring and automatic restore for [Valetudo](https://valetudo.cloud)
on rooted Dreame/Mova robots — for when the robot factory-resets itself and
takes Valetudo with it.

Runs as a single Docker container with a web UI. Built for Unraid, but it's
plain Docker and runs anywhere.

---

## Why this exists

On NAND/eMMC Dreame platforms, Valetudo periodically vanishes: the binary, its
config, the map and the Wi-Fi settings all disappear and `/data` comes back
looking freshly provisioned.

This is widely reported as ext4 corruption with the firmware rebuilding the
filesystem. On the device this tool was developed against — a Mova P10 Pro
Ultra (`r2416`) — **that is not what happens.** There is no `mkfs`, no `fsck`,
and no filesystem recreation anywhere in the path. `/usr/bin/factory_reset.sh`
does:

```sh
rm -rf /data/*  /data/.common  /mnt/misc/config.tar.bz2
tar -xjf ${FACTORY_RESET_PKG} -C /data/
```

A plain `rm -rf`. The trigger is a two-strike watchdog ladder in
`/etc/rc.d/monitor.sh`, keyed on the health of `ava`, the main vendor process:

```
check_ava_alive() fails repeatedly  ->  /data/ava_reboot_cnt reaches 3
    /data/sys_auto_reboot.mark ABSENT   -> touch mark; reboot      (strike 1)
    /data/sys_auto_reboot.mark PRESENT  -> factory_reset.sh monitor_rescue_brick
                                           => rm -rf /data/*       (strike 2)
```

which is why the log line reads:

```
factory reset by monitor rescue brick
```

`monitor_rescue_brick` is a literal argument in the firmware — not a
description of disk damage. Nobody pressed reset and nothing was corrupt.

The mark is cleared in exactly one place: the 03:00 cron job
(`/usr/bin/check_restart_ava.sh`), and only if the robot is idle *and*
responsive at that moment. If it is busy or unhealthy then, the mark survives
indefinitely — which is why wipes look random.

**You cannot patch this.** The rootfs is a read-only squashfs. All you can do
is keep good backups, notice quickly, and put things back. Hence this tool.

---

## What it does

- **Nightly backup** of everything that matters, pulled over SSH
- **Monitoring** every N minutes with four distinct states
- **Notifications** via webhook (Home Assistant, ntfy, Discord, …)
- **Auto-restore** — off by default, opt-in
- **Retention** — keep the newest N archives, prune the rest
- **Web UI** for configuration, manual backup/restore and an event log

### What gets backed up

| Source | Why |
|---|---|
| `/data/valetudo_config.json` | MQTT settings, schedules, everything you configured |
| `/data/_wipe_guard.sh` | wipe-guard, if installed |
| `/data/_root_postboot.sh` | boot hook |
| `/data/log/factory_reset.log` | wipe history — the audit trail |
| `/data/config`, `/data/map` | robot config and maps |
| `/mnt/misc` | calibration, LDS config, consumables |
| `/mnt/private` | **irreplaceable** per-robot identity (did/key/sn/mac/cpuid) |

The 37 MB Valetudo binary is deliberately **not** in the archive — it is always
re-downloadable from GitHub. It is cached separately in `/config` so restores
work with no internet. `/mnt/private` is the part that genuinely cannot be
regenerated.

### Monitor states

| State | Meaning | Action |
|---|---|---|
| `HEALTHY` | SSH ok, binary present, Valetudo running | none |
| `CRASHED` | SSH ok, binary present, not running | restart it |
| `WIPED` | SSH ok, binary **observed missing** | full restore |
| `NO_SSH` | could not talk to the robot | **never acts** |
| `OFFLINE` | not reachable on the network | not an alert |

`NO_SSH` exists for a specific reason. An earlier watchdog classified anything
that wasn't clearly healthy as `WIPED`, using a bare `else`. A brief Wi-Fi
dropout then produced a false "robot was wiped" alert and a spurious recovery
attempt. **A failure to observe is not evidence of a wipe.** The probe now
carries a sentinel so "SSH worked and the binary is gone" is distinguishable
from "SSH did not answer", and unverified states never trigger action.

Verdicts must also repeat (`confirm_samples`, default 2) before anything
happens, because a single poll catches reboots and Wi-Fi roams.

---

## Install

### Unraid

Community Applications → add the template from `unraid/valetudo-restore.xml`,
or add a container manually:

| Setting | Value |
|---|---|
| Repository | `ghcr.io/adman234/valetudo-restore:latest` |
| Port | `8095` → `8080` |
| Path | `/mnt/user/appdata/valetudo-restore` → `/config` |
| Path | `/mnt/user/backups/valetudo` → `/backups` |

### docker compose

```yaml
services:
  valetudo-restore:
    image: ghcr.io/adman234/valetudo-restore:latest
    container_name: valetudo-restore
    restart: unless-stopped
    ports: ["8095:8080"]
    volumes:
      - ./config:/config
      - ./backups:/backups
    environment:
      TZ: "Europe/London"
```

Then open `http://<host>:8095`.

---

## Environment variables

**None of these are strictly required** — the container starts with working
defaults and everything can be configured in the web UI. They exist so a
deployment can be described entirely in a compose file or Unraid template.

### Container paths and runtime

These are the ones that genuinely matter for a container deployment. The two
paths must point at persistent volumes or you lose your settings and backups on
every restart.

| Variable | Default | Purpose |
|---|---|---|
| `VR_CONFIG_DIR` | `/config` | settings.json, event DB, SSH key, cached binary. **Mount a volume.** |
| `VR_BACKUP_DIR` | `/backups` | where archives are written. **Mount a volume.** |
| `VR_PORT` | `8080` | port inside the container |
| `VR_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `TZ` | `UTC` | timezone — decides when the nightly backup actually runs |

### Initial configuration (optional)

These **seed the settings on first run only**. Once `settings.json` exists the
web UI is authoritative, so a stale env var can never silently undo a change you
made in the UI. To re-seed, delete `settings.json` from the config volume.

| Variable | Default | Purpose |
|---|---|---|
| `VR_ROBOT_HOST` | `192.168.50.117` | robot IP or hostname |
| `VR_ROBOT_PORT` | `22` | SSH port |
| `VR_ROBOT_USER` | `root` | SSH user |
| `VR_SSH_KEY_PATH` | `/config/valetudo_key` | private key path inside the container |
| `VR_SSH_TIMEOUT` | `10` | SSH connect timeout (seconds) |
| `VR_BACKUP_ENABLED` | `true` | enable the nightly backup |
| `VR_BACKUP_HOUR` | `2` | hour (0–23) |
| `VR_BACKUP_MINUTE` | `30` | minute (0–59) |
| `VR_KEEP_BACKUPS` | `14` | how many archives to retain |
| `VR_MONITOR_ENABLED` | `true` | enable monitoring |
| `VR_POLL_INTERVAL_MINUTES` | `5` | how often to poll |
| `VR_CONFIRM_SAMPLES` | `2` | consecutive identical verdicts required before acting |
| `VR_AUTO_RESTORE` | `false` | restore automatically on a confirmed wipe |
| `VR_MAX_RESTORE_ATTEMPTS` | `3` | attempts allowed per window |
| `VR_RESTORE_WINDOW_HOURS` | `6` | the window for the above |
| `VR_RESTORE_WIPE_GUARD` | `true` | also reinstall the wipe-guard and boot hook |
| `VR_WEBHOOK_URL` | *(empty)* | notification webhook; empty disables notifications |
| `VR_WEBHOOK_HEADERS` | *(empty)* | extra headers as JSON, e.g. `{"Authorization":"Bearer x"}` |
| `VR_NOTIFY_ON_WIPE` | `true` | notify when a wipe is detected |
| `VR_NOTIFY_ON_CRASH` | `true` | notify when Valetudo has stopped |
| `VR_NOTIFY_ON_RESTORE` | `true` | notify when a restore runs |
| `VR_NOTIFY_ON_BACKUP_FAILURE` | `true` | notify when a backup fails |
| `VR_VALETUDO_ARCH` | `aarch64` | `aarch64`, `armv7` or `amd64` |
| `VR_AUTO_DOWNLOAD_BINARY` | `true` | fetch the release binary when needed |

Booleans accept `1/true/yes/on` (case-insensitive); anything else is false.

**The SSH key is not an environment variable.** Upload it through the UI, or
place the file in the config volume yourself — putting a private key in an env
var leaks it into `docker inspect`, process listings and Unraid's template XML.

Fully-specified example:

```yaml
services:
  valetudo-restore:
    image: ghcr.io/adman234/valetudo-restore:latest
    restart: unless-stopped
    ports: ["8095:8080"]
    volumes:
      - ./config:/config
      - ./backups:/backups
    environment:
      TZ: "Europe/London"
      VR_ROBOT_HOST: "192.168.50.117"
      VR_BACKUP_HOUR: "2"
      VR_BACKUP_MINUTE: "30"
      VR_KEEP_BACKUPS: "14"
      VR_AUTO_RESTORE: "false"
      VR_WEBHOOK_URL: "http://homeassistant:8123/api/webhook/valetudo"
```

---

## Setup

1. **Upload your SSH key** (Settings → Upload SSH key). This is the key you use
   to reach the robot; it is stored at `/config/valetudo_key` mode 0600.
   Key-only auth — passwords are never accepted.
2. **Set the robot's IP** and hit **Test connection**. You want `HEALTHY`.
3. **Cache the binary** so restores work without internet.
4. **Back up now** once, to confirm the whole path works.
5. Decide on **auto-restore**. Off by default — a restore writes to the robot.

Schedule the backup *before* the robot's own nightly reboot (03:00–05:00 on
Dreame firmware), so a wipe during that reboot is captured with fresh data. The
default is 02:30.

---

## Notes from the field

Things that are easy to get wrong on these robots, all handled by this tool:

**No sftp-server.** `scp` fails with `sh: /usr/libexec/sftp-server: not found`.
Files must be streamed through `cat > dest`.

**CRLF kills scripts silently.** BusyBox `ash` cannot parse CRLF and fails with
`syntax error: unexpected end of file (expecting "then")` — the script just
never runs. Shell scripts are normalised to LF on upload.

**`VALETUDO_CONFIG_PATH` matters enormously.** Without it, Valetudo writes its
config to `/tmp`, which is tmpfs — so every setting is silently lost at the next
reboot, looking exactly like the wipe bug. Restores rebuild the boot hook from
`/misc/_root_postboot.sh.tpl` (on the read-only rootfs, so it survives a wipe)
rather than hand-rolling a minimal hook, because the template also sets that
variable, disables Wi-Fi power management and pins the timezone.

**Host keys change after a wipe**, so host-key checking is disabled — pinning
would break precisely when recovery is needed.

**Restores are budgeted.** Repeated failures back off and stop rather than
hammering a robot that is genuinely broken.

---

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | liveness |
| GET | `/api/status` | current state summary |
| POST | `/api/test-connection` | probe the robot now |
| POST | `/api/backup` | back up now |
| POST | `/api/restore` | restore (optional `filename` form field) |
| POST | `/api/monitor-tick` | run one monitoring poll |
| GET | `/api/backups` | list archives |
| GET | `/api/backups/{file}` | download an archive |
| POST | `/api/backups/{file}/delete` | delete an archive |
| GET | `/api/events` | event log |

Webhook payload:

```json
{
  "source": "valetudo-restore",
  "event": "wiped",
  "message": "Valetudo binary is gone - /data was wiped",
  "robot": "192.168.50.117",
  "ts": 1787680000,
  "detail": {}
}
```

---

## Security

This tool holds an SSH private key with root access to a device that has a
camera and a microphone. Keep the `/config` volume private, don't expose the
web UI to the internet, and consider a WAN firewall block for the robot itself.
There is no authentication on the UI — put it behind your reverse proxy if you
need one.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
VR_CONFIG_DIR=./config VR_BACKUP_DIR=./backups \
  python -m uvicorn app.main:app --reload --port 8080
```

## License

MIT
