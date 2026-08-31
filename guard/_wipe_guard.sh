#!/bin/sh
# ---------------------------------------------------------------------------
# wipe-guard : stop the firmware turning an `ava` crash into `rm -rf /data/*`
#
# THE FIRMWARE MECHANISM (see /etc/rc.d/monitor.sh -> release_mode_monitor)
#
#   check_ava_alive() fails repeatedly  ->  /data/ava_reboot_cnt reaches 3
#       /data/sys_auto_reboot.mark ABSENT   -> touch mark; reboot     (strike 1)
#       /data/sys_auto_reboot.mark PRESENT  -> factory_reset.sh monitor_rescue_brick
#                                              => rm -rf /data/*      (strike 2)
#
# HOW THIS BLOCKS IT
#
# monitor.sh tests `[ ! -f ${SYS_AUTO_REBOOT} ]`. A DIRECTORY is not a regular
# file, so holding the mark as a directory makes that test true forever and the
# firmware always takes the reboot branch -- it never calls factory_reset.sh.
# `touch` on a directory only updates mtime, and `rm -f` on one fails, so the
# mark survives both the firmware's touch and the nightly check_restart_ava.sh.
#
# Verified on an r2416 by running a neutered copy of factory_reset.sh with every
# destructive command replaced by an echo.
#
# WHY IT STANDS DOWN
#
# Blocking the wipe forever is not free. On 2026-08-31 `ava` began crashing on
# every boot; the guard held, so no data was lost, but the robot then rebooted
# every ~194s for DAYS because the firmware's own repair was permanently
# blocked. Letting the reset happen fixed ava immediately.
#
# So after ESCALATION_LIMIT consecutive boots where ava never becomes healthy,
# the guard removes the armor and lets the firmware do its thing. Transient
# faults stay protected; a genuinely broken robot is not held hostage. The
# counter resets the moment ava is healthy.
#
# OPTIONAL: create /data/_wipe_guard.block_all to also hold
# /tmp/factory_reset.txt, the mutex factory_reset.sh checks on entry, blocking
# EVERY caller including the physical reset button. Off by default.
#
# TO UNDO:
#   rm -f /data/_wipe_guard.sh /data/_wipe_guard.block_all
#   rmdir /data/sys_auto_reboot.mark ; rm -f /tmp/factory_reset.txt
#   (and remove the guard line from /data/_root_postboot.sh)
# ---------------------------------------------------------------------------

MARK=/data/sys_auto_reboot.mark
CNT=/data/ava_reboot_cnt
LOG=/data/_wipe_guard.log
BLOCK_ALL=/data/_wipe_guard.block_all
ESCALATIONS=/data/_wipe_guard.escalations
STOOD_DOWN=/data/_wipe_guard.stood_down
FR_MUTEX=/tmp/factory_reset.txt

ESCALATION_LIMIT=6          # consecutive unhealthy boots before standing down
HEALTH_GRACE=150            # seconds to let ava come up before judging
INTERVAL=60

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

# Once per boot, after a grace period: is ava alive? If not for long enough,
# stop blocking the firmware's repair.
assess_boot() {
    n=$(cat "$ESCALATIONS" 2>/dev/null || echo 0)
    if ava_healthy; then
        [ "$n" != "0" ] && log "ava healthy - clearing escalation counter (was ${n})"
        echo 0 > "$ESCALATIONS"
        return 0
    fi
    n=$((n + 1))
    echo "$n" > "$ESCALATIONS"
    log "ava UNHEALTHY ${HEALTH_GRACE}s after boot - consecutive failure ${n}/${ESCALATION_LIMIT}"
    if [ "$n" -ge "$ESCALATION_LIMIT" ]; then
        log "STANDING DOWN: ava has failed ${n} boots in a row. Removing the armor so the firmware can factory-reset. Restore from backup afterwards."
        rm -rf "$MARK" 2>/dev/null
        rm -f "$FR_MUTEX" 2>/dev/null
        touch "$STOOD_DOWN" 2>/dev/null
        return 1
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

    # Keep wifi power-save off. The dustbuilder postboot template applies this
    # at ~9s uptime but wlan0 does not associate until ~15s, so the boot-time
    # attempt fails; the driver also re-enables PS on re-association.
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

    # disk-full is an independent factory-reset trigger
    use=$(df /data 2>/dev/null | awk '/\/data$/{gsub("%","",$5); print $5}')
    if [ -n "$use" ] && [ "$use" -ge 80 ]; then
        log "WARNING: /data ${use}% full - monitor_disk_full can also force a factory reset"
    fi

    trim_log
done
