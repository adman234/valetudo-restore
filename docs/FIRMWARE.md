# Firmware notes

How the wipe actually happens on a Mova P10 Pro Ultra (`r2416`), the ideas that were tried and dropped, and the small helper scripts that survived. None of this is needed to use the tool; it is here so the reasoning is not lost.

## What triggers the wipe

On NAND/eMMC Dreame platforms, Valetudo periodically vanishes: the binary, its
config, the map and the Wi-Fi settings all disappear and `/data` comes back
looking freshly provisioned.

This is widely reported as ext4 corruption with the firmware rebuilding the
filesystem. On the device this tool was developed against, a Mova P10 Pro
Ultra (`r2416`), **that is not what happens.** There is no `mkfs`, no `fsck`,
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

`monitor_rescue_brick` is a literal argument in the firmware, not a
description of disk damage. Nobody pressed reset and nothing was corrupt.

The mark is cleared in exactly one place: the 03:00 cron job
(`/usr/bin/check_restart_ava.sh`), and only if the robot is idle *and*
responsive at that moment. If it is busy or unhealthy then, the mark survives
indefinitely, which is why wipes look random.

**You cannot patch this.** The rootfs is a read-only squashfs, so
`monitor.sh` and `factory_reset.sh` cannot be edited.

You can make the firmware unable to reach the wipe, but that turned out to be
the wrong fix. See *Why not block the wipe?* below.

## Recovering from the wipe

The firmware's only repair is "delete everything and hope". It does work: a
wipe cleared a crash loop immediately on 2026-08-31, so the bad state really was
in `/data`. But it takes Valetudo, the map, the room names and the voice
pack with it.

A map **can** be restored, provided the backup is complete (see *Restoring the map* in [REFERENCE.md](REFERENCE.md)). Restoring only `/data/map` does not work: `ava` treats the map as
invalid and discards the slot on the next boot. It needs `/data/ri` and
`/data/DivideMap` too.

So the answer this tool settled on is a complete, verified backup and a fast
restore, not an attempt to stop the firmware.

## Why not block the wipe?

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
| `ava` crash-looping on every boot | Guard held and nothing was lost, but the robot rebooted every ~194s for **days**, because the firmware's own repair was blocked. The wipe fixed `ava` instantly. |
| Wipe at 17:17 | Guard's stand-down had already fired. It delayed the wipe by ~18 minutes and changed nothing. |
| Wipe at 12:15 | Guard did not prevent it. |

Blocking a `rm -rf` you can already recover from, at the cost of hiding a broken
robot behind a reboot loop, is the wrong trade. Complete backups are the better
answer. The code and the option are gone; this section stays so the idea does not
get reinvented.

## What is still installed: wifi-keeper

One piece of the old guard was doing measurable work and survives as
`guard/wifi-keeper.sh`. The dustbuilder boot template does:

```sh
echo 0 > /sys/module/8189fs/parameters/rtw_power_mgnt
iw dev wlan0 set power_save off
```

but `/data/_root_postboot.sh` runs at roughly 9s uptime and `wlan0` does not
associate until about 15s, so **both fail at boot**: `iw` has no interface
to talk to yet. The 8189fs driver also re-enables power management on every
re-association, so a robot that roams between APs drifts back on its own.

Left alone, power-save stays ON, and the robot becomes intermittently
unreachable. That shows up as bogus "robot was wiped" alerts and as
`Socket is closed` part-way through a large SSH upload. `wifi-keeper` re-asserts
both settings every 60s and logs each correction to `/data/wifi-keeper.log`.

### Installing wifi-keeper

```sh
cat guard/wifi-keeper.sh | ssh root@<robot> 'cat > /data/wifi-keeper.sh && chmod +x /data/wifi-keeper.sh'
```

Then add it to the boot hook. The tool does this automatically on restore:

```sh
if [ -x /data/wifi-keeper.sh ]; then
        /data/wifi-keeper.sh > /dev/null 2>&1 &
fi
```

## crash-keeper: evidence that survives the wipe

When `ava` fails a health check, the vendor watchdog tars `ava`'s logs, a
memory history and a system snapshot into `/data/tmp_log.tar.gz`. The wipe
then deletes all of `/data`, so the only record of why `ava` failed is
destroyed by the failure it documents.

`guard/crash-keeper.sh` runs from the boot hook and copies each new tarball,
with the kernel log at that moment, to `/mnt/misc/vr-evidence`. `/mnt/misc` is
the one writable place a wipe does not touch. It also writes one line per boot
(uptime, whether the watchdog mark is armed, `ava`'s memory use, free memory),
so a boot line showing `mark=yes` means the previous reboot was the watchdog's
first strike.

`/mnt/misc` is only about 3 MB and also holds calibration data, so the
evidence is capped at 1 MB, keeps the newest four captures, and is skipped
rather than written if it would leave less than 1 MB free.

A restore installs it (Settings, *crash-keeper*, on by default), and
**Install helper scripts** installs or updates it on a robot that needs no
restore. When a wipe is detected the tool takes a diagnostics capture
straight away, which includes `/mnt/misc/vr-evidence`. Nightly backups carry it
too, inside `misc.tar.gz`.

## The boot hook is not optional

`/data/_root_postboot.sh` is invoked from `/etc/rc.sysinit`:

```sh
[ -f /data/_root_postboot.sh ] && sh /data/_root_postboot.sh
```

That line is the **only** thing on the robot that starts Valetudo. Nothing
in `/etc/rc.d`, `/etc/init.d` or `/etc/crontabs` references it. The hook also
sets `VALETUDO_CONFIG_PATH=/data/valetudo_config.json`; without that env var
Valetudo writes its config to `/tmp`, which is tmpfs, so **every setting is lost
at the next reboot**. This is what caused the "I rebooted and my schedules were
gone" symptom.

A restore therefore always rebuilds the hook from `/misc/_root_postboot.sh.tpl`,
the dustbuilder template, rather than hand-rolling a `/data/valetudo &` line.

## Dead end: miio map recovery

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

## Where per-room floor material lives

It is stored twice, and only one copy is authoritative:

| Layer | Path | Form |
|---|---|---|
| **Per-room setting** | `/data/ri/<slot>.dat2` → `seg_info` | numeric, e.g. `{"4":{"material":6,…}}` |
| **Detection result** | `/data/DivideAI/ai_result/<slot>/ai_floors_large.txt` | named, e.g. `{"id":4,"material":"shorthaired_carpet"}` |

The numeric codes line up with the names: `1` = wood, `2` = ceramic,
`6` = shorthaired_carpet.

Both are captured. `/data/ri` has always been restored as part of the map;
`/data/DivideAI` is now restored alongside it, because restoring the map without
it leaves the detection layer empty and rooms read as generic until the robot
re-derives them on a later clean.

## Notes from the field

Things that are easy to get wrong on these robots, all handled by this tool:

**No sftp-server.** `scp` fails with `sh: /usr/libexec/sftp-server: not found`.
Files must be streamed through `cat > dest`.

**CRLF kills scripts silently.** BusyBox `ash` cannot parse CRLF and fails with
`syntax error: unexpected end of file (expecting "then")`, and the script just
never runs. Shell scripts are normalised to LF on upload.

**`VALETUDO_CONFIG_PATH` matters enormously.** Without it, Valetudo writes its
config to `/tmp`, which is tmpfs, so every setting is silently lost at the next
reboot, looking exactly like the wipe bug. Restores rebuild the boot hook from
`/misc/_root_postboot.sh.tpl` (on the read-only rootfs, so it survives a wipe)
rather than hand-rolling a minimal hook, because the template also sets that
variable, disables Wi-Fi power management and pins the timezone.

**Host keys change after a wipe**, so host-key checking is disabled. Pinning
would break precisely when recovery is needed.

**Restores are budgeted.** Repeated failures back off and stop rather than
hammering a robot that is genuinely broken.
