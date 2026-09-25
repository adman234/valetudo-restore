# Reference

Details of what is backed up and restored, how the monitor decides a robot was wiped, every environment variable, notifications and the HTTP API.

## What gets backed up

| Source | Why |
|---|---|
| `/data/valetudo_config.json` | MQTT settings, schedules, everything you configured |
| `/data/wifi-keeper.sh` | wifi power-save keeper, if installed |
| `/data/_root_postboot.sh` | boot hook |
| `/data/log/factory_reset.log` | the latest wipe entry (each wipe recreates the log, so it only ever holds one) |
| `/data/config` | vendor config, incl. room names and quirks |
| `/data/ri`, `/data/map`, `/data/DivideMap`, `/data/DivideDebug`, `/data/log/map_info.bin` | **the complete map**: all of these, or it will not load |
| `/data/DivideAI` | per-room floor material the robot detected; restored with the map |
| `/mnt/misc` | calibration, LDS config, consumables |
| `/mnt/private` | **irreplaceable** per-robot identity (did/key/sn/mac/cpuid) |
| `/data/personalized_voice` | installed voice packs (optional, on by default) |

### Quirks, system options and voice packs

| Thing | Where it actually lives | Captured? |
|---|---|---|
| **Valetudo settings** (MQTT, timers, web UI auth, NTP, updater…) | `valetudo_config.json` | Yes |
| **Quirks** (carpet sensitivity, detergent, mop frequency…) | vendor state under `/data/config/ava/*` | Yes, via `data_config.tar.gz` |
| **Voice pack selection** | `/data/config/ava/language_in_use` | Yes, via `data_config.tar.gz` |
| **Voice pack audio** | `/data/personalized_voice/<NAME>/` | Yes (set `VR_BACKUP_VOICE_PACK=false` to skip) |
| **Consumable counters** | `/mnt/misc/consumable.json` | Yes, via `misc.tar.gz` |
| **Wi-Fi credentials** | `/data/config/miio/wifi.conf` | Yes, via `data_config.tar.gz` |

Quirks are *not* stored by Valetudo. It reads and writes them straight through
to the vendor process, so they live in the vendor config and are covered by
`data_config.tar.gz`. The voice pack is the one that needed special handling: the
selection is a one-line file in the vendor config, but the audio is several MB
under `/data/personalized_voice`, which a wipe destroys and which cannot be
regenerated without the original download URL.

Voice-pack *installation* in Valetudo takes a URL and a hash. Those are not
persisted anywhere on the robot (only the extracted audio is), which is why
capturing the files matters if you no longer have the link.

The 37 MB Valetudo binary is deliberately **not** in the archive, since it is always
re-downloadable from GitHub. It is cached separately in `/config` so restores
work with no internet. `/mnt/private` is the part that genuinely cannot be
regenerated.

## Monitor states

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

The status card on the dashboard shows the last recorded verdict. Every real
probe records one: the scheduled poll, **Test connection**, and the check that
runs after a restore or a Valetudo restart. The card also refreshes itself every
30 seconds and whenever the tab regains focus. A manual probe updates what is
shown but never advances the confirmation count, so pressing Test connection
cannot make auto-restore act sooner.

## Incomplete backups

A backup taken after a wipe but before a restore captures the wipe, not your
robot. Restoring it would overwrite a recoverable robot with the placeholder
state, and a retention window full of them would eventually delete every backup
that could have helped. So each backup is judged when it is taken, and flagged
**incomplete** when any of these hold:

| Check | Why |
|---|---|
| the probe at backup time found no Valetudo binary, or no config | the robot was wiped (the same test the `WIPED` state uses) |
| `valetudo_config.json`, `data_config.tar.gz` or any part of the map set is missing | it cannot put the robot back |
| it has no named rooms, but the last full backup did | the map is the placeholder a wipe leaves behind; this catches a robot where Valetudo was reinstalled by hand after a wipe |

A flagged backup is shown in red with its reasons, and then:

- **Restore all data from newest full backup** and **map only** from the newest
  use the newest *full* backup and skip newer flagged ones. The result lists what
  was skipped.
- **Auto-restore does not guess.** With *Only auto-restore from the newest backup*
  on (the default), a flagged newest backup pauses auto-restore instead of falling
  back to an older one. The dashboard shows a banner, one `auto_restore_paused`
  notification is sent, and you choose: restore a backup yourself, or mark the
  newest as full to let auto-restore continue. With the setting off, auto-restore
  uses the newest full backup like the manual button.
- **Fewer rooms is fine.** Only a backup with *no* named rooms is flagged (the
  blank map a wipe leaves). A map with fewer rooms than before, after merging
  rooms or a map reset, is judged full.
- **Retention never prunes the newest full backup**, even when it falls outside
  the window.
- You can still restore a flagged backup deliberately from its row. The
  confirmation says why it was flagged.
- If a flag is wrong, for example after deliberately re-mapping, **mark as
  full** overrides it.

The check deliberately ignores archive size and item count, because both move
for legitimate reasons. Uninstalling duststreamer took one archive from 18 items
and 3.8 MB to 17 items and 875 KB with nothing wrong, and turning on the voice
pack adds about 5 MB. Size-based flagging would have marked that backup
incomplete.

Archives taken before this check existed are judged on their contents when the
container starts. They carry no probe result, so only the last two checks apply.

## Environment variables

**None of these are strictly required.** The container starts with working
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
| `TZ` | `UTC` | timezone, which decides when the nightly backup actually runs |

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
| `VR_BACKUP_HOUR` | `2` | hour (0-23) |
| `VR_BACKUP_MINUTE` | `30` | minute (0-59) |
| `VR_KEEP_BACKUPS` | `14` | how many archives to retain |
| `VR_BACKUP_VOICE_PACK` | `true` | include `/data/personalized_voice` (~5 MB gzipped per backup) |
| `VR_MONITOR_ENABLED` | `true` | enable monitoring |
| `VR_POLL_INTERVAL_MINUTES` | `5` | how often to poll |
| `VR_CONFIRM_SAMPLES` | `2` | consecutive identical verdicts required before acting |
| `VR_AUTO_RESTORE` | `false` | restore automatically on a confirmed wipe |
| `VR_MAX_RESTORE_ATTEMPTS` | `3` | attempts allowed per window |
| `VR_RESTORE_WINDOW_HOURS` | `6` | the window for the above |
| `VR_AUTO_RESTORE_NEWEST_ONLY` | `true` | if the newest backup is flagged incomplete, pause auto-restore instead of using an older one |
| `VR_REBOOT_AFTER_RESTORE` | `true` | reboot the robot after every successful restore, manual or automatic |
| `VR_RESTORE_WIFI_KEEPER` | `true` | also reinstall `wifi-keeper.sh` (the boot hook is always rebuilt) |
| `VR_RESTORE_CRASH_KEEPER` | `true` | install `crash-keeper.sh`, which keeps the watchdog's crash logs where a wipe cannot delete them |
| `VR_RESTORE_VENDOR_SETTINGS` | `true` | restore `/data/config/ava` (pet avoidance, obstacle images, room names) |
| `VR_RESTORE_DUSTSTREAMER` | `true` | reinstall duststreamer if the backup has it |
| `VR_DUSTSTREAMER_URL` | *(Hypfer release)* | fallback download when the backup has no copy |
| `VR_VOICE_PACK_URL` | *(empty)* | recorded for rebuilds; Valetudo does not store it |
| `VR_VOICE_PACK_HASH` | *(empty)* | md5 that pairs with the above |
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
place the file in the config volume yourself. Putting a private key in an env
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

## What a restore actually puts back

**Restore all data** writes back everything a wiped robot needs, in this order:

| Step | What |
|---|---|
| 1 | the Valetudo binary (skipped when its md5 already matches) |
| 2 | `valetudo_config.json`: every Valetudo setting |
| 3 | `wifi-keeper.sh`, and `crash-keeper.sh` from the image |
| 4 | the voice pack |
| 5 | vendor settings from `/data/config/ava`, from a curated list: pet avoidance, obstacle images, carpet, mop, room names |
| 6 | duststreamer, from the archive or the configured URL |
| 7 | the boot hook, rebuilt from `/misc/_root_postboot.sh.tpl` |
| 8 | the complete map plus `/data/DivideAI`, with `ava` stopped for the swap and restarted after it (it is restarted even without a map, so the vendor settings take effect) |
| 9 | Valetudo restarted, then a fresh probe so the dashboard shows the new state |
| 10 | the robot rebooted, when *Reboot the robot after a restore* is on (the default) |

Steps 3, 5, 6 and 10 can each be switched off in Settings. With no archive chosen, a
restore uses the **newest full backup** and skips any newer ones flagged
incomplete (see *Incomplete backups*); the result lists what it skipped.

### Rebooting after a restore

Restarting `ava` and Valetudo in place has not always been enough for a
restore to take, so by default the robot is rebooted once a restore succeeds.
That covers every kind: manual or automatic, full or map-only, from a stored
backup or an uploaded one. A failed restore never reboots.

The reboot is preceded by `sync`. The robot is offline for 2-4 minutes, and the
dashboard shows `OFFLINE` for that time rather than the pre-reboot verdict.
A background check then waits for the robot and logs how it came back. It
waits for `HEALTHY` rather than taking the first answer, because a booting
robot accepts SSH before Valetudo has started, which would otherwise look like
a crash. The **Reboot robot** button gets the same treatment.

Turn it off with *Reboot the robot after a restore* in Settings, or
`VR_REBOOT_AFTER_RESTORE=false`.

Some things are captured but never written back automatically:

- `/mnt/misc`: calibration and consumable counters.
- `/mnt/private`: factory identity (did/key/sn/mac/cpuid). It is backed up
  because it cannot be regenerated, and never written back because corrupting
  it can brick the robot. Restore it by hand, deliberately, if you ever truly
  need to.
- `/data/config/miio`: wifi and device identity.

## Restoring the map

**The map is not a JSON file.** `/data/map` is a directory of binary SLAM data
(`app_map.bin`, `fine_large.bin`, `wifi_fine.bin`) alongside a few JSON
descriptors, and a working map needs `/data/ri` and `/data/DivideMap` as well,
so the transportable unit is a backup archive. Valetudo's own map *download*
produces a `ValetudoMap` JSON, which is a derived rendering format: it cannot be
converted back and is not restorable.

Use **map only** on a backup row, or upload an archive under *Restore from a
specific archive*. An archive missing any part of the map set is refused rather
than half-applied, and so is a bare `data_map.tar.gz`.

> An earlier version of this README said a map does not survive a factory
> reset. That was wrong. The backups of the time lacked `/data/ri` and
> `/data/DivideMap`, so `ava` discarded the incomplete map on the next boot.
> With the complete set, a map restored onto a freshly wiped robot loads with
> its room names and floor materials.

Safety properties:

* the robot's current map is moved to `/data/_map_replaced-<timestamp>` first,
  so the operation is reversible (the two most recent are kept)
* `ava` and `miio_client` are stopped for the swap and restarted afterwards,
  and the robot is then rebooted unless *Reboot the robot after a restore* is
  off
* `/data/config/miio` and `/mnt/private` are never touched

## Manual controls

The dashboard has controls Valetudo's own UI does not offer:

| Control | What it does |
|---|---|
| **Test connection** | probes the robot and updates the status card immediately |
| **Restart Valetudo** | stops and relaunches just the Valetudo process |
| **Reboot robot** | reboots the whole machine; restores do this for you by default |
| **map only** (per backup) | restores just the map, see above |
| **mark as full** (per backup) | overrides an incomplete flag, see *Incomplete backups* |

Restart always relaunches with `VALETUDO_CONFIG_PATH` set. Restarting it by hand
without that variable silently moves the config to tmpfs, and every setting is
lost at the next reboot.

## Notifications

Notifications are sent as a JSON `POST` to the webhook URL:

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

`event` is one of:

| Event | Sent when |
|---|---|
| `test` | you press **Send test notification** |
| `wiped` | a confirmed wipe: SSH ok, binary observed missing |
| `wipe_detected` | the robot's `factory_reset.log` holds a new entry |
| `crashed` | Valetudo is installed but not running |
| `restored` | a restore completed |
| `restore_failed` | a restore failed |
| `backup_failed` | a backup failed |
| `auto_restore_paused` | the robot is wiped but the newest backup is incomplete, so auto-restore waits for you |

The firmware recreates `factory_reset.log` on every wipe, so it only ever holds
the latest entry. A new wipe is recognised by that entry changing, not by the
log growing.

### Testing it

Settings > **Send test notification** reports the **HTTP status** rather than a
bare success/failure. That matters: Home Assistant answers `404` for a webhook
id that does not exist. That is the most common misconfiguration, and indistinguishable
from silence otherwise. The test uses *saved* settings, so save first.

### Home Assistant setup

Create an automation with a **Webhook** trigger, note its id, then set the
webhook URL to `http://<ha-host>:8123/api/webhook/<your-id>`.

```yaml
alias: Valetudo alert
trigger:
  - platform: webhook
    webhook_id: valetudo
    allowed_methods: [POST]
    local_only: true
action:
  - service: notify.mobile_app_yourphone
    data:
      title: "Valetudo: {{ trigger.json.event }}"
      message: "{{ trigger.json.message }}"
```

`local_only: true` keeps the webhook reachable only from your LAN, which is what
you want, since the endpoint is unauthenticated.

To alert only on the states that actually need you, filter on the event:

```yaml
condition:
  - condition: template
    value_template: "{{ trigger.json.event in ['wiped', 'wipe_detected', 'restore_failed', 'backup_failed'] }}"
```

Other targets work the same way: ntfy, Gotify and Discord all accept a JSON
POST; use **Extra headers** for anything needing an auth token.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | liveness |
| GET | `/api/status` | current state, plus `incomplete_backups` and `newest_full_backup` |
| POST | `/api/test-connection` | probe the robot now |
| POST | `/api/backup` | back up now |
| POST | `/api/restore` | restore everything; without `filename`, from the newest full backup |
| POST | `/api/monitor-tick` | run one monitoring poll |
| POST | `/api/test-webhook` | send a test notification and report the HTTP status |
| POST | `/api/restore-map` | restore the map from an upload or a stored backup; without `filename`, the newest full one |
| GET | `/api/backups` | list archives, each with `full`, `complete`, `reasons` and `override` |
| GET | `/api/backups/{file}` | download an archive |
| POST | `/api/backups/{file}/delete` | delete an archive |
| POST | `/api/install-helpers` | install or update wifi-keeper and crash-keeper without a restore |
| POST | `/api/capture-diagnostics` | pull crash evidence off the robot now |
| GET | `/api/diagnostics` | list diagnostics captures |
| POST | `/api/backups/{file}/mark-full` | override an incomplete flag (`on=1`) or withdraw it (`on=0`) |
| GET | `/api/events` | event log |
