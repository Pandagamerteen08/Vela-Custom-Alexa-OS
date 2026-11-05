#!/bin/bash
# Build the armv7 root filesystem and pack it as an xz squashfs slot image.
#
# Why a chroot under QEMU: `apk` runs package triggers, which are armv7 binaries.
# You cannot install armv7 packages from an x86 host without an emulator, and
# "just use --root --arch" silently skips triggers that create users and
# symlinks.
#
# Usage:
#   tools/mkrootfs.sh --dry-run                 # plan + package validation only
#   sudo tools/mkrootfs.sh                      # full build -> build/root.squashfs
#   tools/mkrootfs.sh --root /tmp/prepared      # pack an already-prepared tree
#   tools/mkrootfs.sh --variant inittab         # no OpenRC (fallback if RAM is tight)
#   tools/mkrootfs.sh --dev                     # include alsa-utils + apk-tools
#
# Output: build/root.squashfs and build/manifest.json (sha256 + size + build id).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
BUILD="$ROOT/build"
ALPINE_VER=${ALPINE_VER:-3.20}
ALPINE_BRANCH=${ALPINE_BRANCH:-v3.20}
MIRROR=${MIRROR:-https://dl-cdn.alpinelinux.org/alpine}
OUT=${OUT:-$BUILD/root.squashfs}
VARIANT=${VARIANT:-openrc}
TREE="$BUILD/rootfs-tree"
PREPARED=""
DRY=0
DEV=0
MAX_BYTES=$((20 * 1024 * 1024))
QEMU=${QEMU:-/usr/bin/qemu-arm-static}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1; shift ;;
        --root) PREPARED=$2; shift 2 ;;
        --out) OUT=$2; shift 2 ;;
        --variant) VARIANT=$2; shift 2 ;;
        --dev) DEV=1; shift ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

log() { printf '[rootfs] %s\n' "$*"; }
die() { printf '[rootfs] error: %s\n' "$*" >&2; exit 1; }

# Portability probes: python3 is named python on some hosts, and stat -c is
# GNU while stat -f is BSD/macOS. Both are used below.
if [ -z "${PY:-}" ]; then
    if command -v python3 >/dev/null 2>&1; then PY=python3
    elif command -v python >/dev/null 2>&1; then PY=python
    else die "no python3/python on PATH"; fi
fi
fsize() {
    stat -c%s "$1" 2>/dev/null || stat -f%z "$1" 2>/dev/null || \
        wc -c <"$1" | tr -d ' '
}

case "$VARIANT" in openrc|inittab) : ;; *) die "--variant must be openrc or inittab" ;; esac

# --- 1. the package contract ---------------------------------------------
[ -f "$ROOT/rootfs/packages.txt" ] || die "rootfs/packages.txt is missing"
PKGS=$("$PY" "$ROOT/tools/packages.py" apk-args --file "$ROOT/rootfs/packages.txt") || \
    die "packages.txt failed validation (every package needs a justification)"
[ -n "$PKGS" ] || die "package list is empty"

# wpa_supplicant is pinned to 2.9 on purpose; Alpine $ALPINE_VER may not carry it.
log "packages: $PKGS"
case "$PKGS" in
    *"wpa_supplicant=2.9"*)
        log "note: wpa_supplicant 2.9 is not in Alpine $ALPINE_VER; either"
        log "      (a) build it from source (docs/net.md), or"
        log "      (b) point MIRROR/ALPINE_BRANCH at v3.17 for that one package."
        log "      Do not substitute 2.11: RSN replay counters deauth every AP."
        ;;
esac

if [ "$DRY" = "1" ]; then
    log "dry run"
    log "  variant:  $VARIANT"
    log "  alpine:   $ALPINE_VER (armhf)"
    log "  tree:     ${PREPARED:-$TREE}"
    log "  output:   $OUT"
    log "  overlay:  $ROOT/rootfs/overlay"
    log "  required on host: ${PREPARED:+nothing (prepared tree)}${PREPARED:-qemu-arm-static, root, mksquashfs}"
    exit 0
fi

command -v mksquashfs >/dev/null || die "mksquashfs not found (squashfs-tools)"
command -v "${PY:-python3}" >/dev/null || die "python3 not found"

# --- 2. assemble the tree ------------------------------------------------
if [ -n "$PREPARED" ]; then
    [ -d "$PREPARED" ] || die "$PREPARED is not a directory"
    log "using prepared tree $PREPARED (no apk step)"
    TREE="$PREPARED"
else
    [ "$(id -u)" = "0" ] || die "the apk step needs root (chroot + mknod). Use sudo, or --root."
    command -v "$QEMU" >/dev/null || die "$QEMU not found. Install qemu-user-static."
    # The explicit `chroot ... qemu-arm-static` call below works without binfmt,
    # but apk's armv7 post-install triggers need it. Warn instead of dying: in a
    # container without --privileged this is expected, and apk fails loudly on
    # its own if a trigger is actually required.
    if [ ! -e /proc/sys/fs/binfmt_misc/arm ] && \
       [ ! -e /proc/sys/fs/binfmt_misc/qemu-arm ]; then
        log "WARNING: arm binfmt is not registered; apk's armv7 triggers may fail"
        log "         host fix: apt install binfmt-support qemu-user-static && update-binfmts --enable qemu-arm"
    fi

    rm -rf "$TREE"
    mkdir -p "$TREE"
    TARBALL="alpine-minirootfs-$ALPINE_VER.0-armv7.tar.gz"
    if [ ! -f "$BUILD/$TARBALL" ]; then
        log "downloading $TARBALL"
        # Directory listing, because the patch release number moves.
        # `sort -V` is GNU-only, so version-sort with python instead.
        URL=$(curl -fsSL "$MIRROR/$ALPINE_BRANCH/releases/armhf/" | \
              grep -o "alpine-minirootfs-$ALPINE_VER\.[0-9]*-armv7\.tar\.gz" | \
              "$PY" -c 'import sys,re\nv=sorted(set(sys.stdin.read().split()), key=lambda s: [int(x) for x in re.findall(r"[0-9]+", s)] or [0])\nprint(v[-1] if v else "")')
        [ -n "$URL" ] || die "could not find a minirootfs for Alpine $ALPINE_VER; set ALPINE_VER"
        curl -fsSL -o "$BUILD/$TARBALL" "$MIRROR/$ALPINE_BRANCH/releases/armhf/$URL"
    fi
    log "extracting $TARBALL"
    tar -xzf "$BUILD/$TARBALL" -C "$TREE"
    cp "$QEMU" "$TREE/usr/bin/"
    printf '%s\n%s\n' "$MIRROR/$ALPINE_BRANCH/main" "$MIRROR/$ALPINE_BRANCH/community" \
        > "$TREE/etc/apk/repositories"

    log "apk add (chroot, armv7 under qemu)"
    # --no-scripts is NOT used: the triggers are the reason for the chroot.
    chroot "$TREE" /usr/bin/qemu-arm-static /sbin/apk add --no-cache $PKGS \
        || die "apk add failed. Check network, mirrors, and that the pins exist."
    if [ "$DEV" = "1" ]; then
        log "dev extras: alsa-utils apk-tools-static"
        chroot "$TREE" /usr/bin/qemu-arm-static /sbin/apk add --no-cache \
            alsa-utils apk-tools-static || log "warning: dev extras failed (not fatal)"
    fi
    # strip inside the chroot so the right strip binary is used
    chroot "$TREE" /usr/bin/qemu-arm-static /bin/sh -c \
        'for f in $(find /usr/lib /usr/bin /usr/sbin /bin /sbin -type f 2>/dev/null); do
             case "$(head -c4 "$f" 2>/dev/null)" in
                 "$(printf "\177ELF")") strip --strip-unneeded "$f" 2>/dev/null || true ;;
             esac
         done' || log "warning: strip pass had errors (not fatal)"
    rm -f "$TREE/usr/bin/qemu-arm-static"
fi

# --- 3. overlay ----------------------------------------------------------
log "overlaying $ROOT/rootfs/overlay"
cp -a "$ROOT/rootfs/overlay/." "$TREE/"

if [ "$VARIANT" = "inittab" ]; then
    log "variant inittab: using the OpenRC-free inittab"
    cp "$TREE/etc/inittab.busybox" "$TREE/etc/inittab"
    rm -f "$TREE"/etc/init.d/cronos-*
fi

# The squashfs is read-only, so every mountpoint must exist in the image.
for d in proc sys dev data var run tmp mnt store; do
    mkdir -p "$TREE/$d"
done
chmod 1777 "$TREE/tmp"
chmod 755 "$TREE/var" "$TREE/run"

# --- 4. sanity: a rootfs that cannot boot should fail here, not on the panel
log "verifying boot-critical paths"
missing=0
for f in sbin/init bin/busybox etc/inittab; do
    if [ ! -e "$TREE/$f" ]; then
        printf '[rootfs] MISSING: /%s\n' "$f" >&2
        missing=1
    fi
done
[ "$missing" = "0" ] || die "refusing to pack an unbootable tree"
if [ ! -e "$TREE/usr/sbin/cronosd" ] && [ ! -e "$TREE/bin/cronosd" ]; then
    log "note: no cronosd in the tree yet (P4 not built); init will report it and"
    log "      drop to the serial console instead of respawning forever"
fi

# --- 5. pack -------------------------------------------------------------
log "packing squashfs (xz, 128 KiB blocks)"
rm -f "$OUT"
mksquashfs "$TREE" "$OUT" \
    -comp xz -b 131072 -no-xattrs -all-root -noappend -no-progress \
    || die "mksquashfs failed"

SIZE=$(fsize "$OUT")
log "wrote $OUT ($((SIZE / 1024)) KiB)"

"$PY" - "$OUT" "$SIZE" <<'PY'
import hashlib, json, os, subprocess, sys, time
path, size = sys.argv[1], int(sys.argv[2])
h = hashlib.sha256()
with open(path, "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
try:
    rev = subprocess.check_output(["git", "-C", os.path.dirname(path) or ".", "rev-parse", "--short", "HEAD"],
                                  text=True, stderr=subprocess.DEVNULL).strip()
except Exception:
    rev = "nogit"
manifest = {
    "format": 1,
    "image": os.path.basename(path),
    "sha256": h.hexdigest(),
    "size": size,
    "build": rev,
    "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "note": "unsigned local build; production manifests are Ed25519-signed",
}
out = os.path.join(os.path.dirname(path) or ".", "manifest.json")
with open(out, "w") as f:
    json.dump(manifest, f, indent=2)
    f.write("\n")
print(f"[rootfs] manifest: {out}")
print(f"[rootfs] sha256:   {manifest['sha256']}")
PY

if [ "$SIZE" -gt "$MAX_BYTES" ]; then
    printf '[rootfs] GATE FAILED: %s KiB > %s KiB\n' "$((SIZE / 1024))" "$((MAX_BYTES / 1024))" >&2
    log "The 20 MB gate exists because two slots plus boot backups have to fit in"
    log "3 GB with room to spare, and because a rootfs that grows is a project that"
    log "drifted back to TECHO5's 130 MB. Find what grew: du -a | sort -h | tail"
    exit 1
fi
log "gate: PASS ($((SIZE * 100 / MAX_BYTES))% of the 20 MB limit)"
log "next: tools/deploy.sh store $OUT a   (dry-run) or push it from TWRP"
