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
# history there as well.
#
# THE WIPE GUARD
#   The first strike leaves /data/sys_auto_reboot.mark behind, and the second
#   strike only wipes if that mark is still there. The firmware's own 03:00 job
#   (check_restart_ava.sh) deletes it every night. This deletes it as soon as it
#   appears instead, right after keeping the evidence, so a run of crashes can
#   only ever cause strike 1 (a reboot) and never the wipe.
#
#   Loop guard: if crashes keep coming, the robot would reboot over and over.
#   So the mark is cleared at most MAX_STRIKES times in a row. The next one is
#   left in place and the firmware wipes as it always did, which is where
#   valetudo-restore takes over. The count starts again once the robot has
#   been up for HEALTHY_S (6 hours) since a boot. Counters live in $OUT, which
#   survives a wipe; uptime is used rather than the clock, which reads 1970
#   right after boot.
#
#   Files in $OUT: strikes.log (one line per strike), .prevented (strikes
#   cleared, ever), .strikes (in a row), .gaveup (present while standing down).
#   /data/crash-keeper.conf with DISARM=0 turns the guard off; it is re-read
#   every pass, so no restart is needed.
#
# /mnt/misc is small (about 3 MB) and also holds calibration data, so the
# evidence is capped and is never allowed to eat into the space the firmware
# needs.
#
# THE CLOCK
#   The robot's clock reads 1970 until it is set, about 20s after boot. The
#   most valuable capture (the first strike's, copied at boot before the next
#   failure overwrites it) is taken in that window, so captures are numbered
#   and ordered by that number, never by date. The boot line waits for the
#   clock (or two minutes) so it carries a real date.
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
MARK=${CK_MARK:-/data/sys_auto_reboot.mark}
CONF=${CK_CONF:-/data/crash-keeper.conf}
UPTIME=${CK_UPTIME:-/proc/uptime}
MAX_STRIKES=${CK_MAX_STRIKES:-3}     # marks cleared in a row before standing down
HEALTHY_S=${CK_HEALTHY_S:-21600}     # uptime that counts as recovered

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
uptime_s() { cut -d. -f1 "$UPTIME"; }

state() {
    mark=no
    [ -e "$MARK" ] && mark=yes
    a=$(pidof ava 2>/dev/null | cut -d' ' -f1)
    rss=$(awk '/VmRSS/ {print $2}' "/proc/$a/status" 2>/dev/null)
    avail=$(awk '/MemAvailable/ {print $2}' /proc/meminfo 2>/dev/null)
    printf '%s up=%ss mark=%s cnt=%s crashes=%s ava_rss=%skB mem_avail=%skB' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$(uptime_s)" "$mark" \
        "$(cat /data/ava_reboot_cnt 2>/dev/null || echo 0)" \
        "$(cat /tmp/crash_count.log 2>/dev/null || echo 0)" "${rss:-?}" "${avail:-?}"
}

note() {
    echo "$(state) $*" >> "$OUT/boots.log"
    tail -n 300 "$OUT/boots.log" > "$OUT/boots.tmp" 2>/dev/null && mv "$OUT/boots.tmp" "$OUT/boots.log"
}

# Captures are named crash-<seq>-..., zero-padded, so name order is capture
# order even when the clock was wrong. Newest first = reverse name order.
captures_newest_first() { ls -1 "$OUT"/crash-*.tar.gz 2>/dev/null | sort -r; }

prune() {
    n=0
    for f in $(captures_newest_first); do
        n=$((n + 1))
        [ "$n" -gt "$KEEP" ] && rm -f "$f" "$f.dmesg.txt"
    done
    while [ "$(kb_used)" -gt "$BUDGET_KB" ]; do
        oldest=$(captures_newest_first | tail -n 1)
        [ -z "$oldest" ] && break
        rm -f "$oldest" "$oldest.dmesg.txt"
    done
}

strike() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') up=$(uptime_s)s $*" >> "$OUT/strikes.log"
    tail -n 100 "$OUT/strikes.log" > "$OUT/strikes.tmp" 2>/dev/null && mv "$OUT/strikes.tmp" "$OUT/strikes.log"
    note "$*"
}

# One mark is one strike: `handled` stops a mark that is deliberately left in
# place from being counted again on every pass.
handled=0
guard() {
    if [ -f "$OUT/.strikes" ] && [ "$(uptime_s)" -ge "$HEALTHY_S" ]; then
        rm -f "$OUT/.strikes" "$OUT/.gaveup"
        strike "up for ${HEALTHY_S}s: the strike count starts again"
    fi
    if [ ! -e "$MARK" ]; then
        handled=0
        return
    fi
    [ "$handled" = 1 ] && return
    handled=1
    grep -q '^DISARM=0' "$CONF" 2>/dev/null && { strike "strike 1 happened; wipe guard is off, mark left in place"; return; }
    n=$(( $(cat "$OUT/.strikes" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$OUT/.strikes"
    if [ "$n" -gt "$MAX_STRIKES" ]; then
        touch "$OUT/.gaveup"
        strike "strike $n in a row: over the limit of $MAX_STRIKES, mark LEFT in place; the next crash run lets the firmware wipe"
        sync
        return
    fi
    rm -f "$MARK"
    handled=0
    total=$(( $(cat "$OUT/.prevented" 2>/dev/null || echo 0) + 1 ))
    echo "$total" > "$OUT/.prevented"
    sync
    strike "strike $n in a row: watchdog mark cleared, no wipe armed (prevented $total)"
}

# The tarball is only ever overwritten, never deleted, so remember what was
# already kept across reboots instead of copying the same one every boot.
last=$(cat "$OUT/.last" 2>/dev/null)
booted=0
while true; do
    # Copy first: at boot, the tarball in /data is the first strike's, and the
    # next failed check overwrites it.
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
                seq=$(( $(cat "$OUT/.seq" 2>/dev/null || echo 0) + 1 ))
                echo "$seq" > "$OUT/.seq"
                dst="$OUT/crash-$(printf '%05d' "$seq")-$(date +%Y%m%d-%H%M%S)-up$(uptime_s).tar.gz"
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
    # After the copy: the evidence of the strike is safe before its mark goes.
    guard
    if [ "$booted" = 0 ] && { [ "$(date +%Y)" -ge 2024 ] || [ "$(uptime_s)" -ge 120 ]; }; then
        note "boot wipe=\"$(tail -n 1 /data/log/factory_reset.log 2>/dev/null)\""
        booted=1
    fi
    sleep "$INTERVAL"
done
