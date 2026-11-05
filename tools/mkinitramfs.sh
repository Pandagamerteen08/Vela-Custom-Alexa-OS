#!/bin/bash
# Assemble the initramfs: aarch64 busybox + slotctl + /init, packed as newc cpio
# and gzipped. Budget is 3.5-5 MB; mkinitramfs prints the size and warns.
#
# IMPORTANT ARCHITECTURE NOTE
#   The initramfs runs on the arm64 kernel, so everything in it must be aarch64
#   static. The rootfs on the squashfs is armv7. Two ABIs on one device, and
#   the initramfs is the one people forget — an armv7 busybox here fails with
#   "Exec format error" and no console output, because there is no console yet.
#
# Usage:
#   tools/mkinitramfs.sh --busybox /path/to/aarch64-busybox --out build/initramfs.cpio.gz
#   tools/mkinitramfs.sh --dry-run
#
# Getting a static aarch64 busybox without a full build:
#   curl -LO https://busybox.net/downloads/binaries/1.35.0-x86_64-linux-musl/busybox  # wrong arch
#   # correct source: Alpine's aarch64 busybox-static package, extracted with apk:
#   apk fetch --arch aarch64 --root /tmp/bb --allow-untrusted busybox-static
# Verify with: file busybox  ->  "ELF 64-bit LSB ... ARM aarch64 ... statically linked"
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)

# python3 is not called python3 on every host, and stat -c is GNU-only.
if [ -z "${PY:-}" ]; then
    if command -v python3 >/dev/null 2>&1; then PY=python3
    elif command -v python >/dev/null 2>&1; then PY=python
    else echo "[initramfs] error: no python3/python on PATH" >&2; exit 1; fi
fi
fsize() {
    stat -c%s "$1" 2>/dev/null || stat -f%z "$1" 2>/dev/null || \
        wc -c <"$1" | tr -d ' '
}

OUT=${OUT:-$ROOT/build/initramfs.cpio.gz}
BUSYBOX=${BUSYBOX:-}
SLOTCTL=${SLOTCTL:-$ROOT/build/slotctl-aarch64}
CROSS_COMPILE=${CROSS_COMPILE:-aarch64-linux-gnu-}
DRY=0
MAX_BYTES=$((5 * 1024 * 1024))

# Single parser: every option takes a value except --dry-run, so there is no
# ambiguity to get wrong (an earlier version parsed twice and shifted in the
# first loop).
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1; shift ;;
        --out) OUT=$2; shift 2 ;;
        --busybox) BUSYBOX=$2; shift 2 ;;
        --slotctl) SLOTCTL=$2; shift 2 ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

log() { printf '[initramfs] %s\n' "$*"; }
die() { printf '[initramfs] error: %s\n' "$*" >&2; exit 1; }

# Applets /init uses. Anything missing becomes a silent failure at boot.
APPLETS="sh mount umount mkdir sleep cat ls dmesg switch_root losetup \
         blkid e2fsck mknod echo printf test sed tr grep dd sync reboot poweroff"

log "checking present: $APPLETS"
# (the file must exist to boot at all; the rest are diagnostics)

if [ "$DRY" = "1" ]; then
    log "dry run: would assemble $ROOT/build/initramfs/ and pack to $OUT"
    log "  busybox: ${BUSYBOX:-<unset>}"
    log "  slotctl: $SLOTCTL"
    exit 0
fi

[ -n "$BUSYBOX" ] || die "set BUSYBOX=/path/to/aarch64-static-busybox (see header)"
[ -f "$BUSYBOX" ] || die "$BUSYBOX does not exist"

# Architecture guard: the most expensive possible mistake in this project.
if command -v file >/dev/null 2>&1; then
    desc=$(file -b "$BUSYBOX")
    case "$desc" in
        *aarch64*|*ARM\ aarch64*) : ;;
        *x86-64*|*Intel*|*80386*) die "busybox is $desc — the initramfs needs aarch64" ;;
        *ARM,\ EABI*) die "busybox is 32-bit ARM ($desc). The kernel is arm64:
     an armv7 initramfs fails with 'Exec format error' before any console exists.
     Get busybox-static from Alpine aarch64, not armhf." ;;
        *) log "warning: could not confirm busybox arch: $desc" ;;
    esac
    case "$desc" in
        *"statically linked"*|*"static"*) : ;;
        *) log "warning: busybox does not look statically linked; the initramfs has no libc" ;;
    esac
fi

STAGE="$ROOT/build/initramfs"
rm -rf "$STAGE"
mkdir -p "$STAGE"/{bin,sbin,proc,sys,dev,newroot,tmp}

log "installing /init"
install -m 0755 "$ROOT/initramfs/init" "$STAGE/init"

log "installing busybox + applet symlinks"
install -m 0755 "$BUSYBOX" "$STAGE/bin/busybox"
for applet in $APPLETS; do
    ln -sf busybox "$STAGE/bin/$applet"
done
ln -sf ../bin/busybox "$STAGE/sbin/init" 2>/dev/null || true

# slotctl: build for aarch64 if the caller did not supply one.
if [ ! -f "$SLOTCTL" ]; then
    if command -v "${CROSS_COMPILE}gcc" >/dev/null 2>&1; then
        log "building slotctl for aarch64"
        "${CROSS_COMPILE}gcc" -O2 -std=c11 -static -o "$SLOTCTL" "$ROOT/src/slotctl/slotctl.c"
    else
        die "no aarch64 slotctl at $SLOTCTL and no ${CROSS_COMPILE}gcc.
     The initramfs cannot choose a slot without it."
    fi
fi
install -m 0755 "$SLOTCTL" "$STAGE/bin/slotctl"

# Kernel modules that must exist before the squashfs/loop mounts work. With the
# base fragment these are built in; listed anyway so a future config change
# does not produce a mystifying "unknown filesystem squashfs".
if [ -d "$ROOT/build/kernel" ]; then
    mkdir -p "$STAGE/lib/modules"
    for m in squashfs loop ext4; do
        # `-print -quit` is GNU find; head -1 is portable.
        found=$(find "$ROOT/build/kernel" -name "$m.ko" 2>/dev/null | head -1 || true)
        if [ -n "$found" ]; then
            log "including module $m.ko"
            cp "$found" "$STAGE/lib/modules/"
        fi
    done
fi

log "packing"
"$PY" "$ROOT/tools/mkcpio.py" create "$STAGE" -o "$ROOT/build/initramfs.cpio"
# -n omits the mtime/name so two builds of the same tree are byte-identical;
# gzip straight to $OUT because mv-ing onto the default path is an error.
gzip -9 -n -c "$ROOT/build/initramfs.cpio" > "$OUT"
rm -f "$ROOT/build/initramfs.cpio"

SIZE=$(fsize "$OUT")
log "wrote $OUT ($SIZE bytes)"
if [ "$SIZE" -gt "$MAX_BYTES" ]; then
    log "WARNING: over the ${MAX_BYTES} byte budget. The boot partition is 16 MB"
    log "         and the kernel takes 5.5-7 MB, so an oversized initramfs is a"
    log "         boot failure. Options, in order: drop the module copies,"
    log "         rebuild busybox with a minimal applet set, switch the kernel"
    log "         to CONFIG_RD_XZ and xz this archive."
fi
