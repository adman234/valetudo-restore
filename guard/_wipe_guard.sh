#!/bin/sh
# ---------------------------------------------------------------------------
# wipe-guard : keep an `ava` crash from costing you /data
#
# THE FIRMWARE MECHANISM (see /etc/rc.d/monitor.sh -> release_mode_monitor)
#
#   check_ava_alive() fails repeatedly  ->  /data/ava_reboot_cnt reaches 3
#       /data/sys_auto_reboot.mark ABSENT   -> touch mark; reboot     (strike 1)
#       /data/sys_auto_reboot.mark PRESENT  -> factory_reset.sh monitor_rescue_brick
#                                              => rm -rf /data/*      (strike 2)
#
# The firmware's only repair is "delete everything and hope". That does work --
# on 2026-08-31 a wipe fixed a crash loop immediately, so the bad state really
# was somewhere in /data -- but it takes Valetudo, the map, the room names and
# the voice pack with it. And the map cannot be restored afterwards: ava
# validates map slots against the cloud platform (DeletePlatformInconsistMaps in
# node_lidar_slam.so) and discards any it does not recognise, so a restored map
# is deleted, or ignored if you make it undeletable.
#
# So this guard does something narrower than the firmware, in stages.
#
# HOW IT BLOCKS THE WIPE
#
# monitor.sh tests `[ ! -f ${SYS_AUTO_REBOOT} ]`. A DIRECTORY is not a regular
# file, so holding the mark as a directory makes that test true forever and the
# firmware always takes the reboot branch -- it never calls factory_reset.sh.
# `touch` on a directory only updates mtime, and `rm -f` on one fails, so the
# mark survives both the firmware's touch and the nightly check_restart_ava.sh.
#
# THE REPAIR LADDER
#
# Blocking the wipe alone is not enough: on 2026-08-31 it turned a broken ava
# into a reboot loop that ran for days. So when ava fails to come up over
# consecutive boots, the guard repairs progressively, least destructive first,
# and only surrenders to the firmware as a last resort:
#
#   boots 1..2   nothing - reboots are cheap and most faults are transient
#   boot  3      TIER 1: quarantine /data/config/ava   (ava regenerates it)
#                        -> keeps the map, wifi, Valetudo, voice pack
#   boot  5      TIER 2: also quarantine /data/map     (SLAM state)
#                        -> keeps wifi, Valetudo, its config and voice pack
#   boot  8      STAND DOWN: remove the armor and let the firmware wipe
#
# The factory package (/misc/data.tar.bz2) contains no ava/ directory at all --
# just empty skeletons -- which is why moving ava/ aside is sufficient: ava
# rebuilds it on next start, exactly as it does after a real factory reset.
#
# HARD SAFETY RULES
#
#   * /data/config/miio is NEVER touched. It holds wifi.conf and the device
#     identity; clearing it puts the robot off the network and out of reach.
#   * /mnt/private is NEVER touched. Factory identity; corrupting it can brick.
#   * Repairs QUARANTINE (move) rather than delete, so everything is
#     recoverable and diagnosable afterwards.
#
# OPTIONAL BELT-AND-BRACES
#
# Create /data/_wipe_guard.block_all to also hold /tmp/factory_reset.txt, the
# mutex factory_reset.sh checks on entry, blocking EVERY caller including the
# physical reset button. Off by default; it disables deliberate resets too.
#
# TO UNDO EVERYTHING:
#   rm -f /data/_wipe_guard.sh /data/_wipe_guard.block_all
#   rmdir /data/sys_auto_reboot.mark ; rm -f /tmp/factory_reset.txt
#   (and remove the guard line from /data/_root_postboot.sh)
# ---------------------------------------------------------------------------

MARK=/data/sys_auto_reboot.mark
CNT=/data/ava_reboot_cnt
LOG=/data/_wipe_guard.log
STATE=/data/_wipe_guard.state
BLOCK_ALL=/data/_wipe_guard.block_all
ESCALATIONS=/data/_wipe_guard.escalations
REPAIRS=/data/_wipe_guard.repairs
STOOD_DOWN=/data/_wipe_guard.stood_down
QUARANTINE=/data/_wipe_guard.quarantine
FR_MUTEX=/tmp/factory_reset.txt

TIER1_AT=3                  # quarantine /data/config/ava
TIER2_AT=5                  # additionally quarantine /data/map
STAND_DOWN_AT=8             # give up; let the firmware factory-reset
HEALTH_GRACE=150            # seconds to let ava come up before judging
INTERVAL=60
KEEP_QUARANTINES=4

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG" 2>/dev/null; }

trim_log() {
    [ -f "$LOG" ] || return 0
    sz=$(wc -c < "$LOG" 2>/dev/null)
    [ -n "$sz" ] && [ "$sz" -gt 65536 ] && {
        tail -c 32768 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
    }
    return 0
}

ava_healthy() {
    timeout 15 avacmd media '{"type":"media","cmd":"status_get","no_log":1}' \
        2>/dev/null | grep -q '"ret":"ok"'
}

did_repair() { grep -q "^$1\$" "$REPAIRS" 2>/dev/null; }
mark_repair() { echo "$1" >> "$REPAIRS"; }

prune_quarantine() {
    [ -d "$QUARANTINE" ] || return 0
    n=$(ls -1 "$QUARANTINE" 2>/dev/null | wc -l)
    [ "$n" -le "$KEEP_QUARANTINES" ] && return 0
    ls -1 "$QUARANTINE" 2>/dev/null | head -n $((n - KEEP_QUARANTINES)) | \
    while read -r old; do
        rm -rf "$QUARANTINE/$old" 2>/dev/null
        log "quarantine: pruned old $old"
    done
}

# Hold the mark as a directory so monitor.sh can never reach the wipe branch.
armor_mark() {
    [ -d "$MARK" ] && return 0
    if [ -e "$MARK" ]; then
        log "ARMOR: $MARK was a regular file (strike-1 had fired) - converting to a directory"
        rm -f "$MARK" 2>/dev/null
    fi
    if mkdir -p "$MARK" 2>/dev/null; then
        log "ARMOR: $MARK is now a directory - the firmware cannot reach factory_reset.sh"
        rm -f "$CNT" 2>/dev/null
        return 0
    fi
    log "ARMOR FAILED: could not create $MARK as a directory"
    return 1
}

armor_block_all() {
    [ -f "$BLOCK_ALL" ] || return 0
    [ -f "$FR_MUTEX" ] && return 0
    touch "$FR_MUTEX" 2>/dev/null && \
        log "ARMOR: created $FR_MUTEX - factory_reset.sh will refuse every caller"
}

# --- TIER 1 ---------------------------------------------------------------
# Move the vendor robot state aside. ava rebuilds /data/config/ava on next
# start. Keeps /data/map, /data/config/miio (wifi!), Valetudo and the voice pack.
repair_tier1() {
    dest="$QUARANTINE/tier1-$(date '+%Y%m%d-%H%M%S')"
    mkdir -p "$dest" 2>/dev/null || { log "TIER1: cannot create $dest"; return 1; }
    if [ ! -d /data/config/ava ]; then
        log "TIER1: /data/config/ava does not exist - nothing to repair"
        return 1
    fi
    sz=$(du -sk /data/config/ava 2>/dev/null | cut -f1)
    mv /data/config/ava "$dest/ava" 2>/dev/null || {
        log "TIER1 FAILED: could not move /data/config/ava"; return 1; }
    mark_repair tier1
    log "TIER1 REPAIR: quarantined /data/config/ava (${sz}KB) -> $dest"
    log "TIER1: map, wifi, Valetudo and voice pack left untouched. ava will rebuild its config. Rebooting."
    prune_quarantine
    sync
    reboot
    return 0
}

# --- TIER 2 ---------------------------------------------------------------
# Also move the SLAM map aside. Losing the map is what a factory reset would
# cost anyway; this still keeps wifi, Valetudo, its settings and the voice pack.
repair_tier2() {
    dest="$QUARANTINE/tier2-$(date '+%Y%m%d-%H%M%S')"
    mkdir -p "$dest" 2>/dev/null || { log "TIER2: cannot create $dest"; return 1; }
    [ -d /data/config/ava ] && mv /data/config/ava "$dest/ava" 2>/dev/null
    if [ -d /data/map ]; then
        sz=$(du -sk /data/map 2>/dev/null | cut -f1)
        chattr -R -i /data/map 2>/dev/null
        mv /data/map "$dest/map" 2>/dev/null && \
            log "TIER2 REPAIR: quarantined /data/map (${sz}KB) -> $dest"
        mkdir -p /data/map 2>/dev/null
    fi
    mark_repair tier2
    log "TIER2: wifi, Valetudo, its config and the voice pack left untouched. You will need a fresh mapping run. Rebooting."
    prune_quarantine
    sync
    reboot
    return 0
}

stand_down() {
    log "STANDING DOWN: ava has failed ${1} consecutive boots and tier-1/tier-2 repairs did not help."
    log "Removing the armor so the firmware can factory-reset. Valetudo, config and voice pack will be lost - restore them afterwards."
    rm -rf "$MARK" 2>/dev/null
    rm -f "$FR_MUTEX" 2>/dev/null
    touch "$STOOD_DOWN" 2>/dev/null
}

# Once per boot, after a grace period: judge ava and escalate if needed.
assess_boot() {
    n=$(cat "$ESCALATIONS" 2>/dev/null || echo 0)

    if ava_healthy; then
        if [ "$n" != "0" ]; then
            log "ava healthy - clearing escalation counter (was ${n})"
            if [ -f "$REPAIRS" ]; then
                log "RECOVERED after: $(tr '\n' ' ' < "$REPAIRS")"
                rm -f "$REPAIRS"
            fi
        fi
        echo 0 > "$ESCALATIONS"
        return 0
    fi

    n=$((n + 1))
    echo "$n" > "$ESCALATIONS"
    log "ava UNHEALTHY ${HEALTH_GRACE}s after boot - consecutive failure ${n}"

    if [ "$n" -ge "$STAND_DOWN_AT" ]; then
        stand_down "$n"
        return 1
    fi
    if [ "$n" -ge "$TIER2_AT" ] && ! did_repair tier2; then
        repair_tier2
        return 1
    fi
    if [ "$n" -ge "$TIER1_AT" ] && ! did_repair tier1; then
        repair_tier1
        return 1
    fi
    if [ "$n" -lt "$TIER1_AT" ]; then
        log "holding - ${n}/${TIER1_AT} reboots before the first repair"
    else
        log "holding - waiting to see whether the last repair took (repairs so far: $(tr '\n' ' ' < "$REPAIRS" 2>/dev/null))"
    fi
    return 0
}

log "=== wipe-guard started (pid $$) ==="
if [ -f "$STOOD_DOWN" ]; then
    log "stood down previously - not re-arming. Delete $STOOD_DOWN to re-enable."
else
    armor_mark
    armor_block_all
fi

(
    sleep "$HEALTH_GRACE"
    [ -f "$STOOD_DOWN" ] || assess_boot
) &

while true; do
    sleep "$INTERVAL"

    if [ ! -f "$STOOD_DOWN" ]; then
        armor_mark
        armor_block_all
    fi

    # --- keep wifi power-save off -------------------------------------------
    # The dustbuilder postboot template applies this at ~9s uptime, but wlan0
    # does not associate until ~15s, so the boot-time attempt fails; the driver
    # also re-enables PS on re-association. Flaky wifi makes the robot look
    # unreachable and produces bogus alerts.
    PM=/sys/module/8189fs/parameters/rtw_power_mgnt
    if [ -e "$PM" ]; then
        PM_OLD=$(cat "$PM" 2>/dev/null)
        if [ "$PM_OLD" != "0" ]; then
            echo 0 > "$PM" 2>/dev/null && log "wifi: rtw_power_mgnt was ${PM_OLD}, reset to 0"
        fi
    fi
    if iw dev wlan0 get power_save 2>/dev/null | grep -qi "power save: on"; then
        iw dev wlan0 set power_save off 2>/dev/null && log "wifi: re-disabled power_save on wlan0"
    fi

    # --- disk-full is an independent factory-reset trigger -------------------
    use=$(df /data 2>/dev/null | awk '/\/data$/{gsub("%","",$5); print $5}')
    if [ -n "$use" ] && [ "$use" -ge 80 ]; then
        log "WARNING: /data ${use}% full - monitor_disk_full can also force a factory reset"
    fi

    trim_log
done
