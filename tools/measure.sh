#!/bin/sh
# measure.sh — the RAM and flash gate, run ON the device.
#
# Every size number in the design document is an estimate until this prints
# real values. Run it at boot+5min, then again in 24h; the difference is the
# leak, and a leak is a failed build no matter how small the absolute number.
#
#   ./measure.sh            one report
#   ./measure.sh watch      append a sample every 60s to /data/log/ram.log
#   ./measure.sh flash      what the slots and boot images actually cost
set -u

MEMTOTAL=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
MEMAVAIL=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
SLAB=$(awk '/^Slab:/{print $2}' /proc/meminfo)
CACHED=$(awk '/^Cached:/{print $2}' /proc/meminfo)
USED=$(( (MEMTOTAL - MEMAVAIL) / 1024 ))

case "${1:-report}" in
watch)
    mkdir -p /data/log
    while :; do
        printf '%s uptime=%s used_mib=%s avail_kib=%s slab_kib=%s\n' \
            "$(date -u +%FT%TZ)" "$(cut -d. -f1 /proc/uptime)" \
            "$(( (MEMTOTAL - MEMAVAIL) / 1024 ))" "$MEMAVAIL" "$SLAB" \
            >> /data/log/ram.log
        # cap the log at 4 MB so a forgotten watch cannot fill /data
        [ "$(stat -c%s /data/log/ram.log)" -gt 4194304 ] && \
            tail -c 2097152 /data/log/ram.log > /data/log/ram.log.tmp && \
            mv /data/log/ram.log.tmp /data/log/ram.log
        sleep 60
    done
    ;;

flash)
    echo "== flash =="
    for f in /store/slots/a/root.squashfs /store/slots/b/root.squashfs \
             /store/boot/*.img; do
        [ -f "$f" ] && printf '  %-40s %8s KiB\n' "$f" "$(( $(stat -c%s "$f") / 1024 ))"
    done
    echo "  /store used: $(df -k /store | awk 'NR==2{print $3" KiB of "$2" KiB"}')"
    echo "  /data  used: $(df -k /data  2>/dev/null | awk 'NR==2{print $3" KiB of "$2" KiB"}')"
    echo "  rootfs is squashfs ro: $(mount | awk '/type squashfs/{print $3}')"
    printf '  mmc sectors written: %s\n' "$(awk '{print $10}' /sys/block/mmcblk0/stat 2>/dev/null)"
    ;;

report|*)
    echo "== memory =="
    printf '  MemTotal     %6s MiB\n' "$(( MEMTOTAL / 1024 ))"
    printf '  MemAvailable %6s MiB\n' "$(( MEMAVAIL / 1024 ))"
    printf '  used (total-avail) %4s MiB   <- the number the gate is about\n' "$USED"
    printf '  Cached       %6s MiB\n' "$(( CACHED / 1024 ))"
    printf '  Slab         %6s MiB\n' "$(( SLAB / 1024 ))"
    if [ "$USED" -lt 100 ]; then
        echo "  GATE: PASS (< 100 MiB)"
    else
        echo "  GATE: FAIL ($USED MiB >= 100 MiB) — see the shrink list in docs"
    fi
    echo
    echo "== processes (RSS, largest first) =="
    # No ps_mem on this image: sum statm resident pages per pid.
    for p in /proc/[0-9]*; do
        pid=${p#/proc/}
        rss=$(awk '{print $2}' "$p/statm" 2>/dev/null) || continue
        [ -n "$rss" ] || continue
        comm=$(cat "$p/comm" 2>/dev/null) || comm=?
        printf '%8s %8s KiB  %s\n' "$pid" "$(( rss * 4 ))" "$comm"
    done | sort -k2 -n -r | head -15
    echo
    echo "== top slab caches =="
    head -1 /proc/slabinfo 2>/dev/null
    sort -k3 -n -r /proc/slabinfo 2>/dev/null | head -8
    echo
    echo "== uptime / thermal =="
    printf '  uptime %s s\n' "$(cut -d. -f1 /proc/uptime)"
    for z in /sys/class/thermal/thermal_zone*/temp; do
        [ -r "$z" ] && printf '  %s = %s\n' "$z" "$(cat "$z")"
    done 2>/dev/null | head -6
    echo
    echo "== boot =="
    printf '  cmdline: %s\n' "$(cat /proc/cmdline)"
    printf '  kernel:  %s\n' "$(uname -r)"
    printf '  slot:    %s\n' "$(slotctl get 2>/dev/null || echo 'slotctl not installed (expected on the squashfs build until P4)')"
    ;;
esac
