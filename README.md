# valetudo-restore

Backs up a rooted Dreame or Mova robot vacuum running
[Valetudo](https://github.com/Hypfer/Valetudo), notices when the robot factory-resets
itself, and puts everything back, map included, in one click.

Runs as a single Docker container with a web UI. Built for Unraid, but it is plain
Docker and runs anywhere.

## Backstory

This started with a Mova P10 Pro Ultra (`r2416`) rooted with
[Dustbuilder](https://builder.dontvacuum.me/) and running Valetudo. Every so often it
would come back looking brand new: Valetudo gone, settings gone, Wi-Fi gone, and the
map with all its room names and floor types gone with them. The common explanation
online is ext4 corruption.

Digging through the firmware showed something else. When the vendor process `ava`
fails its health check enough times, a watchdog reboots once, and on the second strike
runs `factory_reset.sh`, which is a plain `rm -rf /data/*`. Nothing is corrupt and
nobody pressed reset. The flag that arms the second strike is only cleared by a 3 AM
cron job, and only if the robot is idle then, which is why the wipes look random.

The firmware is a read-only squashfs, so the watchdog cannot be patched. Blocking the
wipe was tried and dropped: it left the robot stuck in a reboot loop for days instead
of letting the reset fix `ava`. What works is a complete, verified backup and a fast
restore. The full investigation is in [docs/FIRMWARE.md](docs/FIRMWARE.md).

## What it does

- **Nightly backup** over SSH of Valetudo's config, the vendor config (room names,
  quirks, voice pack choice), the complete map, calibration data, the voice pack audio
  and the robot's factory identity.
- **Monitoring** every few minutes. It tells a wiped robot apart from one that is just
  off Wi-Fi, and never acts on a failed check alone.
- **One-click restore** of everything a wiped robot needs, including a map that loads
  with its rooms and floor materials. Auto-restore is available but off by default.
- **Incomplete-backup detection**, so a backup taken after a wipe is never restored or
  allowed to push good backups out of retention.
- **Notifications** by webhook to Home Assistant, ntfy, Discord and similar.
- **Helper scripts** installed on the robot: `wifi-keeper` keeps Wi-Fi power saving off,
  and `crash-keeper` saves the watchdog's crash logs somewhere a wipe cannot delete them.

## Install

### Unraid

Add the template from [`unraid/valetudo-restore.xml`](unraid/valetudo-restore.xml), or
add a container by hand:

| Setting | Value |
|---|---|
| Repository | `ghcr.io/adman234/valetudo-restore:latest` |
| Port | `8095` > `8080` |
| Path | `/mnt/user/appdata/valetudo-restore` > `/config` |
| Path | `/mnt/user/backups/valetudo` > `/backups` |

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
      TZ: "America/Chicago"
```

Then open `http://<host>:8095`. No environment variables are required; everything can
be set in the web UI. The optional ones are listed in
[docs/REFERENCE.md](docs/REFERENCE.md#environment-variables).

## Setup

1. **Upload your SSH key** in Settings. It is stored at `/config/valetudo_key` with mode
   0600. Only key authentication is supported.
2. **Set the robot's IP** and press **Test connection**. You want `HEALTHY`.
3. **Cache the Valetudo binary** so restores work without internet.
4. **Back up now** once to confirm the whole path works.
5. Decide on **auto-restore**. It is off by default because a restore writes to the robot.

Schedule the backup before the robot's own nightly reboot (03:00 to 05:00 on Dreame
firmware). The default is 02:30. [docs/QUICKSTART.md](docs/QUICKSTART.md) shows how to
test the monitor by stopping Valetudo on purpose.

## Security

This tool holds an SSH key with root access to a device that has a camera and a
microphone. The web UI has no authentication, so keep it on your LAN or behind a
reverse proxy, and keep the `/config` volume private.

**Backup archives contain secrets in plain text**: the Valetudo web UI password, MQTT
credentials, the miio device token and `authorized_keys`. Treat a backup file the same
way you treat the SSH key.

## More

- [docs/REFERENCE.md](docs/REFERENCE.md): what is backed up and restored, monitor
  states, incomplete backups, environment variables, notifications and Home Assistant
  setup, the HTTP API.
- [docs/FIRMWARE.md](docs/FIRMWARE.md): how the wipe happens, dead ends, and the helper
  scripts.

## Credits

[Valetudo](https://github.com/Hypfer/Valetudo) and duststreamer are by Hypfer. Rooting
and the boot hook template come from [Dustbuilder](https://builder.dontvacuum.me/) by
Dennis Giese. This project only backs up and restores what they make possible.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
VR_CONFIG_DIR=./config VR_BACKUP_DIR=./backups \
  python -m uvicorn app.main:app --reload --port 8080
```

## License

MIT
