#!/bin/sh
# ---------------------------------------------------------------------------
# wifi-keeper : keep wifi power-save off on Dreame/Mova robots
#
# This is all that remains of the old wipe-guard. The guard also held
# /data/sys_auto_reboot.mark as a directory to stop the firmware reaching
# factory_reset.sh, and that part has been removed: over three incidents it
# failed to prevent one wipe, turned a broken ava into a multi-day reboot loop,
# and in the end only delayed a wipe by ~18 minutes before standing down. It was
# costing more uptime than it saved, and complete backups make a wipe
# recoverable anyway.
#
# The wifi job, by contrast, measurably works.
#
# WHY IT IS NEEDED
#
# The dustbuilder boot template does:
#
#     echo 0 > /sys/module/8189fs/parameters/rtw_power_mgnt
#     iw dev wlan0 set power_save off
#
# but /data/_root_postboot.sh runs at roughly 9s uptime and wlan0 does not
# associate until about 15s, so both fail at boot - `iw` has no interface to
# talk to yet. The 8189fs driver also re-enables power management on every
# re-association, and this robot roams between two APs.
#
# Left alone, power-save stays ON. That makes the robot intermittently
# unreachable, which shows up as bogus "robot was wiped" alerts and as
# "Socket is closed" part-way through a large SSH upload.
#
# INSTALL
#   place at /data/wifi-keeper.sh, chmod +x, and add to /data/_root_postboot.sh:
#     if [ -x /data/wifi-keeper.sh ]; then
#             /data/wifi-keeper.sh > /dev/null 2>&1 &
#     fi
#
# REMOVE
#   rm -f /data/wifi-keeper.sh /data/wifi-keeper.pid
#   (and drop the block from /data/_root_postboot.sh)
# ---------------------------------------------------------------------------

LOG=/data/wifi-keeper.log
PIDFILE=/data/wifi-keeper.pid
INTERVAL=60

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG" 2>/dev/null; }

# One instance only: the boot hook and a restore can both launch this.
if [ -f "$PIDFILE" ]; then
    old=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$old" ] && [ "$old" != "$$" ] && [ -d "/proc/$old" ]; then
        exit 0
    fi
fi
echo $$ > "$PIDFILE" 2>/dev/null

log "=== wifi-keeper started (pid $$) ==="

while true; do
    PM=/sys/module/8189fs/parameters/rtw_power_mgnt
    if [ -e "$PM" ]; then
        PM_OLD=$(cat "$PM" 2>/dev/null)
        if [ "$PM_OLD" != "0" ]; then
            echo 0 > "$PM" 2>/dev/null && \
                log "rtw_power_mgnt was ${PM_OLD}, reset to 0"
        fi
    fi
    if iw dev wlan0 get power_save 2>/dev/null | grep -qi "power save: on"; then
        iw dev wlan0 set power_save off 2>/dev/null && \
            log "re-disabled power_save on wlan0"
    fi

    # keep the log bounded
    sz=$(wc -c < "$LOG" 2>/dev/null)
    [ -n "$sz" ] && [ "$sz" -gt 32768 ] && {
        tail -c 16384 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
    }

    sleep "$INTERVAL"
done
