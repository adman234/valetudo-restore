#!/bin/sh
# ---------------------------------------------------------------------------
# crash-keeper : keep the vendor watchdog's crash evidence out of the wipe's reach
#
# /etc/rc.d/monitor.sh asks ava for its media status every ~45s. On each
# failed check it tars /tmp/log (ava's own logs, a memory history in
# ava_vm.log, a sysmon snapshot) into /data/tmp_log.tar.gz. Three failures in
# a row reboot the robot; three more after that run factory_reset.sh, which
# deletes all of /data, that tarball included. So the one record of why ava
# failed is destroyed by the failure it documents.
#
# This copies every new tarball to /mnt/misc, which the wipe does not touch,
# together with the kernel log at that moment, and keeps a one-line-per-boot
# history there as well. A boot line showing mark=yes means the previous
# reboot was the watchdog's first strike.
#
# /mnt/misc is small (about 3 MB) and also holds calibration data, so the
# evidence is capped and is never allowed to eat into the space the firmware
# needs.
#
# INSTALL
#   place at /data/crash-keeper.sh, chmod +x, and add to /data/_root_postboot.sh:
#     if [ -x /data/crash-keeper.sh ]; then
#             /data/crash-keeper.sh > /dev/null 2>&1 &
#     fi
#
# REMOVE
#   rm -f /data/crash-keeper.sh; rm -rf /mnt/misc/vr-evidence
#   (and drop the block from /data/_root_postboot.sh)
# ---------------------------------------------------------------------------

# Every path and limit can be overridden, which is how it is tested without
# touching the real ones.
SRC=${CK_SRC:-/data/tmp_log.tar.gz}
FS=${CK_FS:-/mnt/misc}
OUT=${CK_OUT:-$FS/vr-evidence}
PIDFILE=${CK_PID:-/tmp/crash-keeper.pid}
BUDGET_KB=${CK_BUDGET_KB:-1024}      # total size of $OUT
MIN_FREE_KB=${CK_MIN_FREE_KB:-1024}  # never leave $FS with less than this free
KEEP=${CK_KEEP:-4}                   # newest crash captures kept
INTERVAL=${CK_INTERVAL:-5}

# One instance only: the boot hook and a restore can both launch this.
if [ -f "$PIDFILE" ]; then
    old=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$old" ] && [ "$old" != "$$" ] && [ -d "/proc/$old" ]; then
        exit 0
    fi
fi
echo $$ > "$PIDFILE"

# The boot hook runs early. /mnt itself is tmpfs, so writing before $FS is
# mounted would put the evidence in RAM, where the next reboot loses it.
waited=0
until grep -q " $FS " /proc/mounts; do
    [ "$waited" -ge 120 ] && exit 1
    sleep 2
    waited=$((waited + 2))
done
mkdir -p "$OUT" || exit 1

kb_used() { du -sk "$OUT" 2>/dev/null | cut -f1; }
kb_free() { df -k "$FS" 2>/dev/null | awk 'NR==2 {print $4}'; }

state() {
    mark=no
    [ -e /data/sys_auto_reboot.mark ] && mark=yes
    a=$(pidof ava 2>/dev/null | cut -d' ' -f1)
    rss=$(awk '/VmRSS/ {print $2}' "/proc/$a/status" 2>/dev/null)
    avail=$(awk '/MemAvailable/ {print $2}' /proc/meminfo 2>/dev/null)
    printf '%s up=%ss mark=%s cnt=%s crashes=%s ava_rss=%skB mem_avail=%skB' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$(cut -d. -f1 /proc/uptime)" "$mark" \
        "$(cat /data/ava_reboot_cnt 2>/dev/null || echo 0)" \
        "$(cat /tmp/crash_count.log 2>/dev/null || echo 0)" "${rss:-?}" "${avail:-?}"
}

note() {
    echo "$(state) $*" >> "$OUT/boots.log"
    tail -n 300 "$OUT/boots.log" > "$OUT/boots.tmp" 2>/dev/null && mv "$OUT/boots.tmp" "$OUT/boots.log"
}

prune() {
    n=0
    for f in $(ls -1t "$OUT"/crash-*.tar.gz 2>/dev/null); do
        n=$((n + 1))
        [ "$n" -gt "$KEEP" ] && rm -f "$f" "$f.dmesg.txt"
    done
    while [ "$(kb_used)" -gt "$BUDGET_KB" ]; do
        oldest=$(ls -1t "$OUT"/crash-*.tar.gz 2>/dev/null | tail -n 1)
        [ -z "$oldest" ] && break
        rm -f "$oldest" "$oldest.dmesg.txt"
    done
}

note "boot wipe=\"$(tail -n 1 /data/log/factory_reset.log 2>/dev/null)\""

# The tarball is only ever overwritten, never deleted, so remember what was
# already kept across reboots instead of copying the same one every boot.
last=$(cat "$OUT/.last" 2>/dev/null)
while true; do
    if [ -f "$SRC" ]; then
        m=$(stat -c %Y "$SRC" 2>/dev/null)
        if [ -n "$m" ] && [ "$m" != "$last" ]; then
            size=$(( ($(stat -c %s "$SRC" 2>/dev/null || echo 0) + 1023) / 1024 ))
            free=$(kb_free)
            [ -z "$free" ] && free=0
            if [ "$size" -gt "$BUDGET_KB" ]; then
                note "evidence is ${size}KB, over the ${BUDGET_KB}KB budget; not kept"
            elif [ $((free - size)) -lt "$MIN_FREE_KB" ]; then
                note "evidence is ${size}KB and would leave $FS under ${MIN_FREE_KB}KB free; not kept"
            else
                dst="$OUT/crash-$(date +%Y%m%d-%H%M%S)-up$(cut -d. -f1 /proc/uptime).tar.gz"
                cp "$SRC" "$dst"
                dmesg 2>/dev/null | tail -n 80 > "$dst.dmesg.txt"
                sync
                note "kept $(basename "$dst") (${size}KB)"
                prune
            fi
            last=$m
            echo "$m" > "$OUT/.last"
        fi
    fi
    sleep "$INTERVAL"
done
