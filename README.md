# valetudo-restore

Survive a rooted Dreame/Mova robot factory-resetting itself — **without** losing
your map.

The firmware's answer to a crashing `ava` is to `rm -rf /data`. That destroys
Valetudo, its settings, the voice pack, and your map with all its room names,
zones and floor materials. Restoring the map is possible but fiddly, and getting
it wrong silently discards the slot on the next boot. This backs up everything
that matters nightly, notices the moment a wipe happens, and puts it all back —
map included — in one click.

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

**You cannot patch this.** The rootfs is a read-only squashfs, so
`monitor.sh` and `factory_reset.sh` cannot be edited.

You can, however, make the firmware unable to reach the wipe. See below.

---

## Recovering from the wipe

The firmware's only repair is "delete everything and hope". It does work &mdash; a
wipe cleared a crash loop immediately on 2026-08-31, so the bad state really was
in `/data` &mdash; but it takes Valetudo, the map, the room names and the voice
pack with it.

A map **can** be restored, provided the backup is complete &mdash; see *Restoring
the map* below. Restoring only `/data/map` does not work: `ava` treats the map as
invalid and discards the slot on the next boot. It needs `/data/ri` and
`/data/DivideMap` too.

So the answer this tool settled on is a complete, verified backup and a fast
restore &mdash; not an attempt to stop the firmware.

### Why not block the wipe? (a dead end worth documenting)

`monitor.sh` decides between rebooting and wiping with:

```sh
if [ ! -f ${SYS_AUTO_REBOOT} ]; then   # /data/sys_auto_reboot.mark
    touch ${SYS_AUTO_REBOOT}; reboot   # strike 1
else
    /usr/bin/factory_reset.sh monitor_rescue_brick   # strike 2 - the wipe
fi
```

`-f` tests for a *regular file*, so holding the mark as a **directory** makes
that test true forever and the firmware never calls `factory_reset.sh`.
`touch` on a directory only updates mtime and `rm -f` on one fails, so the mark
survives both the firmware's `touch` and the nightly `check_restart_ava.sh`.

It works exactly as described. **It was still removed**, because over three
incidents it was net-negative:

| Incident | Outcome |
|---|---|
| `ava` crash-looping on every boot | Guard held, nothing lost &mdash; but the robot rebooted every ~194s for **days**, because the firmware's own repair was blocked. The wipe fixed `ava` instantly. |
| Wipe at 17:17 | Guard's stand-down had already fired. It delayed the wipe by ~18 minutes and changed nothing. |
| Wipe at 12:15 | Guard did not prevent it. |

Blocking a `rm -rf` you can already recover from, at the cost of hiding a broken
robot behind a reboot loop, is the wrong trade. Complete backups are the better
answer. The code and the option are gone; this section stays so the idea does not
get reinvented.

### What is still installed: wifi-keeper

One piece of the old guard was doing measurable work and survives as
`guard/wifi-keeper.sh`. The dustbuilder boot template does:

```sh
echo 0 > /sys/module/8189fs/parameters/rtw_power_mgnt
iw dev wlan0 set power_save off
```

but `/data/_root_postboot.sh` runs at roughly 9s uptime and `wlan0` does not
associate until about 15s, so **both fail at boot** &mdash; `iw` has no interface
to talk to yet. The 8189fs driver also re-enables power management on every
re-association, so a robot that roams between APs drifts back on its own.

Left alone, power-save stays ON, and the robot becomes intermittently
unreachable. That shows up as bogus "robot was wiped" alerts and as
`Socket is closed` part-way through a large SSH upload. `wifi-keeper` re-asserts
both settings every 60s and logs each correction to `/data/wifi-keeper.log`.

### Installing it

```sh
cat guard/wifi-keeper.sh | ssh root@<robot> 'cat > /data/wifi-keeper.sh && chmod +x /data/wifi-keeper.sh'
```

Then add it to the boot hook &mdash; the tool does this automatically on restore:

```sh
if [ -x /data/wifi-keeper.sh ]; then
        /data/wifi-keeper.sh > /dev/null 2>&1 &
fi
```

### The boot hook is not optional

`/data/_root_postboot.sh` is invoked from `/etc/rc.sysinit`:

```sh
[ -f /data/_root_postboot.sh ] && sh /data/_root_postboot.sh
```

That line is the **only** thing on the robot that starts Valetudo &mdash; nothing
in `/etc/rc.d`, `/etc/init.d` or `/etc/crontabs` references it. The hook also
sets `VALETUDO_CONFIG_PATH=/data/valetudo_config.json`; without that env var
Valetudo writes its config to `/tmp`, which is tmpfs, so **every setting is lost
at the next reboot**. This is what caused the "I rebooted and my schedules were
gone" symptom.

A restore therefore always rebuilds the hook from `/misc/_root_postboot.sh.tpl`,
the dustbuilder template, rather than hand-rolling a `/data/valetudo &` line.

---

## What it does

- **Nightly backup** of everything that matters, pulled over SSH
- **Monitoring** every N minutes with five distinct states
- **Notifications** via webhook (Home Assistant, ntfy, Discord, …)
- **Auto-restore** — off by default, opt-in
- **Retention** — keep the newest N archives, prune the rest
- **Complete map restore** — `/data/ri` + `/data/map` + `/data/DivideMap`, which
  is the only combination `ava` accepts
- **Diagnostics capture** — pulls the crash logs off the robot *before* the
  firmware deletes them
- **wifi-keeper** — keeps wifi power-save off, which the boot template fails to do
- **Web UI** for configuration, manual backup/restore and an event log

Backups still matter: they carry Valetudo's config, timers, MQTT, the voice
pack and — importantly — your **room names**, which live in
`/data/config/ava/ava_SchedulePositionInfo.conf` and are readable straight out
of an archive. They are what makes a re-map bearable if it ever comes to that.

### What gets backed up

| Source | Why |
|---|---|
| `/data/valetudo_config.json` | MQTT settings, schedules, everything you configured |
| `/data/wifi-keeper.sh` | wifi power-save keeper, if installed |
| `/data/_root_postboot.sh` | boot hook |
| `/data/log/factory_reset.log` | wipe history — the audit trail |
| `/data/config` | vendor config, incl. room names and quirks |
| `/data/ri`, `/data/map`, `/data/DivideMap`, `/data/DivideDebug`, `/data/log/map_info.bin` | **the complete map** — all of these, or it will not load |
| `/mnt/misc` | calibration, LDS config, consumables |
| `/mnt/private` | **irreplaceable** per-robot identity (did/key/sn/mac/cpuid) |
| `/data/personalized_voice` | installed voice packs (optional, on by default) |

### Quirks, system options and voice packs

| Thing | Where it actually lives | Captured? |
|---|---|---|
| **Valetudo settings** (MQTT, timers, web UI auth, NTP, updater…) | `valetudo_config.json` | ✅ |
| **Quirks** (carpet sensitivity, detergent, mop frequency…) | vendor state under `/data/config/ava/*` | ✅ via `data_config.tar.gz` |
| **Voice pack selection** | `/data/config/ava/language_in_use` | ✅ via `data_config.tar.gz` |
| **Voice pack audio** | `/data/personalized_voice/<NAME>/` | ✅ (set `VR_BACKUP_VOICE_PACK=false` to skip) |
| **Consumable counters** | `/mnt/misc/consumable.json` | ✅ via `misc.tar.gz` |
| **Wi-Fi credentials** | `/data/config/miio/wifi.conf` | ✅ via `data_config.tar.gz` |

Quirks are *not* stored by Valetudo — it reads and writes them straight through
to the vendor process, so they live in the vendor config and are covered by
`data_config.tar.gz`. The voice pack is the one that needed special handling: the
selection is a one-line file in the vendor config, but the audio is several MB
under `/data/personalized_voice`, which a wipe destroys and which cannot be
regenerated without the original download URL.

Voice-pack *installation* in Valetudo takes a URL and a hash. Those are not
persisted anywhere on the robot — only the extracted audio is — which is why
capturing the files matters if you no longer have the link.

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
| `VR_BACKUP_VOICE_PACK` | `true` | include `/data/personalized_voice` (~5 MB gzipped per backup) |
| `VR_MONITOR_ENABLED` | `true` | enable monitoring |
| `VR_POLL_INTERVAL_MINUTES` | `5` | how often to poll |
| `VR_CONFIRM_SAMPLES` | `2` | consecutive identical verdicts required before acting |
| `VR_AUTO_RESTORE` | `false` | restore automatically on a confirmed wipe |
| `VR_MAX_RESTORE_ATTEMPTS` | `3` | attempts allowed per window |
| `VR_RESTORE_WINDOW_HOURS` | `6` | the window for the above |
| `VR_RESTORE_WIFI_KEEPER` | `true` | also reinstall `wifi-keeper.sh` (the boot hook is always rebuilt) |
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

## What a restore actually puts back

A backup captures more than an automatic restore writes back, deliberately.

| Restored automatically | Captured, but restored only on request |
|---|---|
| Valetudo binary | `/data/map` — map, rooms, no-go zones |
| `valetudo_config.json` (all settings) | `/data/config` — vendor/ava config |
| `wifi-keeper.sh` | `/mnt/misc` — calibration |
| `_root_postboot.sh` boot hook | `/mnt/private` — **never written** |

So after an auto-restore you get Valetudo and every one of its settings back.
The robot will still need a fresh mapping run: see the caveat under *Restoring
the map* — map data does not survive a factory reset even when restored.

`/mnt/private` holds factory identity (did/key/sn/mac/cpuid). It is backed up
because it cannot be regenerated, and never written back because corrupting it
can brick the robot. Restore it by hand, deliberately, if you ever truly need to.

### A dead end worth documenting: miio map recovery

The vendor firmware has a map-recovery path reachable over the local miio
protocol (`miio_client` listens on UDP 54321 and Valetudo does not occupy it).
Writing siid 6 / piid 10 with
`{"map_url","map_id","req_type","force_type"}` returns code 0 and the robot
downloads the URL itself with wget. That much was verified on a real r2416.

It was still abandoned. The recovery archive format is undocumented; nine
candidate layouts built from the robot's own map data were all downloaded and
all rejected. More to the point, **it is not needed**: copying the map paths
directly, as maploader does, works and is far simpler.

The real cause of every failed map restore before this was mundane - the backup
was missing `/data/ri` and `/data/DivideMap`, so ava discarded the incomplete
map. The miio investigation was an elaborate theory built on top of that bug.

### Restoring the map

**The map is not a JSON file.** `/data/map` is a directory of binary SLAM data
(`app_map.bin`, `fine_large.bin`, `wifi_fine.bin`) alongside a few JSON
descriptors, so the transportable unit is a `.tar.gz`. Valetudo's own map
*download* produces a `ValetudoMap` JSON — a derived rendering format that
cannot be converted back and is not restorable.

Dashboard → **Restore map only**. Either restore from the newest backup, or
upload an archive — both a full backup archive and a bare `data_map.tar.gz`
are accepted, and anything else is rejected with an explanation.

> **The map does NOT survive a factory reset.** Tested on an r2416 on
> 2026-08-31: after a wipe, restoring `/data/map` puts the files back, but `ava`
> deletes the map slot directory on the next boot and Valetudo reports
> `"defaultMap": true`. This happens with or without the matching
> `/data/config/ava/mult_map.json` registry entry restored — both were tried.
> The SLAM map is bound to vendor state the reset clears, so an orphaned slot is
> garbage-collected.
>
> **After a factory reset, plan on a fresh mapping run.** Map restore is useful
> for putting a map back on a robot that still has its vendor state — for
> example after an accidental map reset — not for recovering from a wipe.

Safety properties:

* the robot's current map is copied to `/data/map.bak-<timestamp>` first, so the
  operation is reversible
* `/mnt/private` is never touched
* **reboot the robot afterwards** — `ava` holds the map files open and will not
  pick up changes made underneath it

### Manual controls

The dashboard has three controls Valetudo's own UI does not offer:

| Button | What it does |
|---|---|
| **Restart Valetudo** | stops and relaunches just the Valetudo process |
| **Reboot robot** | reboots the whole machine — do this after restoring a map |
| **Restore map only** | see above |

Restart always relaunches with `VALETUDO_CONFIG_PATH` set. Restarting it by hand
without that variable silently moves the config to tmpfs, and every setting is
lost at the next reboot.

---

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
| `wipe_detected` | a new line appeared in the robot's `factory_reset.log` |
| `crashed` | Valetudo is installed but not running |
| `restored` | a restore completed |
| `restore_failed` | a restore failed |
| `backup_failed` | a backup failed |

### Testing it

Settings → **Send test notification** reports the **HTTP status** rather than a
bare success/failure. That matters: Home Assistant answers `404` for a webhook
id that does not exist — the most common misconfiguration, and indistinguishable
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
you want — the endpoint is unauthenticated.

To alert only on the states that actually need you, filter on the event:

```yaml
condition:
  - condition: template
    value_template: "{{ trigger.json.event in ['wiped', 'wipe_detected', 'restore_failed', 'backup_failed'] }}"
```

Other targets work the same way — ntfy, Gotify and Discord all accept a JSON
POST; use **Extra headers** for anything needing an auth token.

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
| POST | `/api/test-webhook` | send a test notification and report the HTTP status |
| POST | `/api/restore-map` | restore `/data/map` from an upload or a stored backup |
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

**Backup archives contain secrets in plaintext.** A backup includes the Valetudo
web UI basic-auth password, MQTT broker credentials, the miio cloud device token
and `authorized_keys`. Anyone holding a backup file effectively has full access
to the robot. Keep `/backups` off any share you would hand round, and treat a
downloaded archive the same way you would treat the SSH key itself.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
VR_CONFIG_DIR=./config VR_BACKUP_DIR=./backups \
  python -m uvicorn app.main:app --reload --port 8080
```

## License

MIT
