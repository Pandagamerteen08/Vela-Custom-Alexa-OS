#!/bin/bash
# deploy.sh — flash a vela artifact, with the safety rails that this
# device needs. Dry-run is the default: you have to ask twice to write flash.
#
# Why the paranoia: most cronos units have no BROM USBDL entry, so a bad write
# to preloader/LK/TEE is a dead device with no software recovery, and the
# anti-rollback fuses are one-way. The only partitions this script will touch
# are boot (p9), system (p12) and userdata (p16). Everything else is refused,
# including recovery, which must keep TWRP because it is the only local
# recovery path and boots 32-bit kernels only.
#
# Usage:
#   tools/deploy.sh boot build/boot.img             # dry run, prints the commands
#   tools/deploy.sh boot build/boot.img --yes       # actually flash
#   tools/deploy.sh store build/root.squashfs a     # push a slot image via adb
#   tools/deploy.sh wifi MyNetwork 'passphrase'      # provision first-boot Wi-Fi
#   tools/deploy.sh status
#   tools/deploy.sh restore build/boot.good.img --yes
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
BOOT_PART=9
STORE_DEV=/dev/block/mmcblk0p12
DATA_DEV=/dev/block/mmcblk0p16
BOOT_PART_BYTES=$((16 * 1024 * 1024))

CMD=${1:-}
shift || true
CONFIRM=0
for arg in "$@"; do
    [ "$arg" = "--yes" ] && CONFIRM=1
done
DRYRUN=$((1 - CONFIRM))

log() { printf '[deploy] %s\n' "$*"; }
die() { printf '[deploy] REFUSED: %s\n' "$*" >&2; exit 1; }

# This runs on the host with the cable, which is very often a Mac or Windows
# laptop: stat -c is GNU, stat -f is BSD/macOS.
fsize() {
    stat -c%s "$1" 2>/dev/null || stat -f%z "$1" 2>/dev/null || \
        wc -c <"$1" | tr -d ' '
}
if [ -z "${PY:-}" ]; then
    if command -v python3 >/dev/null 2>&1; then PY=python3
    elif command -v python >/dev/null 2>&1; then PY=python
    else die "no python3/python on PATH (needed to validate boot images)"; fi
fi
run() {
    if [ "$DRYRUN" = "1" ]; then
        printf '[dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

need_fastboot() {
    command -v fastboot >/dev/null || die "fastboot not found (android-tools / android-sdk)"
}
need_adb() {
    command -v adb >/dev/null || die "adb not found"
}

case "$CMD" in
status)
    need_fastboot
    fastboot devices
    echo
    echo "partition table (read-only; never write p1-p8, p10, p11, p13-p15):"
    fastboot getvar partition-size:boot 2>&1 | sed 's/^/  /'
    fastboot getvar partition-size:system 2>&1 | sed 's/^/  /'
    echo
    echo "boot partition: 16 MB  |  system: 3.0 GB  |  userdata: 3.9 GB"
    ;;

boot)
    IMG=${1:-}
    [ -n "$IMG" ] || die "usage: deploy.sh boot <boot.img> [--yes]"
    [ -f "$IMG" ] || die "$IMG does not exist"
    SIZE=$(fsize "$IMG")
    [ "$SIZE" -le "$BOOT_PART_BYTES" ] || die "$IMG is $SIZE bytes; p9 is 16 MB. LK will not boot it."
    "$PY" "$ROOT/tools/mkbootimg.py" info "$IMG" | sed 's/^/  /' || die "not a valid boot image"
    cat <<'EOF'

  THIS WRITES THE boot PARTITION (p9). Before proceeding:
    * TWRP is still installed on recovery (p10) and boots — it is your undo.
    * You have a copy of the working image: /store/boot/good.img, or a dump
      taken with `adb shell dd if=/dev/block/mmcblk0p9 of=/sdcard/boot-backup.img`
    * The device is charged / on mains. A power cut during a p9 write is
      recoverable with fastboot (LK is intact); a power cut during an LK or
      preloader write is not.
EOF
    need_fastboot
    run fastboot flash boot "$IMG"
    run fastboot reboot
    log "after reboot: if the panel stays dark, the kernel did not start."
    log "  next step: flash the dev image (build/boot.img.dev) and watch serial"
    log "  rollback:   fastboot flash boot /store/boot/good.img  (from TWRP/adb)"
    ;;

store)
    IMG=${1:-}
    SLOT=${2:-}
    [ -n "$IMG" ] && [ -n "$SLOT" ] || die "usage: deploy.sh store <root.squashfs> <a|b> [--yes]"
    [ -f "$IMG" ] || die "$IMG does not exist"
    case "$SLOT" in a|b) : ;; *) die "slot must be a or b" ;; esac
    SIZE=$(fsize "$IMG")
    [ "$SIZE" -gt 0 ] || die "empty image"
    log "pushing $IMG ($SIZE bytes) to slot $SLOT"
    need_adb
    run adb push "$IMG" "/tmp/root.squashfs.new"
    # Write to a temp name, fsync, rename: a half-written root.squashfs in the
    # slot being booted is a rescue-shell boot.
    run adb shell "mkdir -p $STORE_DEV-mnt; mount -t ext4 $STORE_DEV /mnt 2>/dev/null || true"
    run adb shell "mkdir -p /mnt/slots/$SLOT"
    run adb shell "cp /tmp/root.squashfs.new /mnt/slots/$SLOT/root.squashfs.new"
    run adb shell "sync"
    run adb shell "mv /mnt/slots/$SLOT/root.squashfs.new /mnt/slots/$SLOT/root.squashfs"
    run adb shell "sync"
    log "arm the slot with 3 trial boots:  adb shell slotctl pending && adb shell reboot"
    ;;

wifi)
    SSID=${1:-}
    PSK=${2:-}
    [ -n "$SSID" ] && [ -n "$PSK" ] || \
        die "usage: deploy.sh wifi <ssid> <psk|64-hex> [--yes]"
    [ ${#SSID} -le 32 ] || die "SSID is longer than 32 bytes"
    # Same rule the device enforces, so bad credentials fail here and not at boot.
    if [ ${#PSK} -eq 64 ]; then
        case "$PSK" in *[!0-9a-fA-F]*) die "a 64-character PSK must be hex" ;; esac
    elif [ ${#PSK} -lt 8 ] || [ ${#PSK} -gt 63 ]; then
        die "PSK must be 8-63 characters, or exactly 64 hex digits"
    fi
    log "provisioning /data/wifi.conf for SSID \"$SSID\""
    need_adb
    # The PSK goes through a 0600 temp file and is deleted on exit: it must not
    # land in shell history, a repo, or a leftover file on the build host.
    TMPCONF=$(umask 077; mktemp "${TMPDIR:-/tmp}/vela-wifi.XXXXXX")
    trap 'rm -f "$TMPCONF"' EXIT
    printf 'SSID=%s\nPSK=%s\n' "$SSID" "$PSK" > "$TMPCONF"
    run adb push "$TMPCONF" /data/wifi.conf
    run adb shell chmod 600 /data/wifi.conf
    run adb shell "rc-service vela-net restart 2>/dev/null || /etc/init.d/vela-net restart"
    log "check it with: adb shell 'wpa_cli -i wlan0 status' (expect wpa_state=COMPLETED)"
    ;;

restore)
    IMG=${1:-}
    [ -n "$IMG" ] || die "usage: deploy.sh restore <boot.img> [--yes]"
    log "restoring a known-good boot image"
    exec "$0" boot "$IMG" ${1:+} $([ "$CONFIRM" = "1" ] && echo --yes)
    ;;

*)
    sed -n '2,20p' "$0"
    exit 2
    ;;
esac
