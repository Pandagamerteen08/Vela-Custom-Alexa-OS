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
#   tools/mkrootfs.sh --no-wpa                  # skip the from-source supplicant
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
# wpa_supplicant is built from source: no Alpine branch from v3.16 up ships 2.9,
# and 2.10+ deauths every AP on this driver. Override WPA_SRC_TARBALL to build
# offline; the checksum is what makes a silent version swap impossible.
WPA_VER=${WPA_VER:-2.9}
WPA_SHA256=${WPA_SHA256:-fcbdee7b4a64bea8177973299c8c824419c413ec2e3a95db63dd6a5dc3541f17}
WPA_URL=${WPA_URL:-https://w1.fi/releases/wpa_supplicant-$WPA_VER.tar.gz}
WPA_CONFIG=$ROOT/rootfs/wpa-$WPA_VER.config
WPA_SRC_TARBALL=${WPA_SRC_TARBALL:-}
BUILD_WPA=${BUILD_WPA:-1}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1; shift ;;
        --root) PREPARED=$2; shift 2 ;;
        --out) OUT=$2; shift 2 ;;
        --variant) VARIANT=$2; shift 2 ;;
        --dev) DEV=1; shift ;;
        --no-wpa) BUILD_WPA=0; shift ;;
        -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
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

# wpa_supplicant is built from source in the chroot (build_wpa below), not
# installed by apk: no Alpine branch from v3.16 up ships 2.9. A list that still
# names it is a mistake, and this says why instead of leaving apk to report an
# unsatisfiable pin.
log "packages: $PKGS"
case "$PKGS" in
    *wpa_supplicant*)
        die "packages.txt must not list wpa_supplicant: it is built from source (rootfs/wpa-$WPA_VER.config), not installed by apk. See docs/net.md."
        ;;
esac

if [ "$DRY" = "1" ]; then
    log "dry run"
    log "  variant:  $VARIANT"
    log "  alpine:   $ALPINE_VER (armhf)"
    log "  tree:     ${PREPARED:-$TREE}"
    log "  output:   $OUT"
    log "  overlay:  $ROOT/rootfs/overlay"
    if [ "$BUILD_WPA" = "1" ]; then
        log "  wpa:      build wpa_supplicant $WPA_VER from source ($WPA_CONFIG)"
    else
        log "  wpa:      skipped (--no-wpa): the image will have no supplicant"
    fi
    log "  required on host: ${PREPARED:+nothing (prepared tree)}${PREPARED:-qemu-arm-static, root, mksquashfs}"
    exit 0
fi

command -v mksquashfs >/dev/null || die "mksquashfs not found (squashfs-tools)"
command -v "${PY:-python3}" >/dev/null || die "python3 not found"

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d' ' -f1
    else
        "$PY" -c 'import hashlib,sys
h = hashlib.sha256()
with open(sys.argv[1], "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
print(h.hexdigest())' "$1"
    fi
}

# wpa_supplicant 2.9, built from source inside the chroot.
#
# Not `apk add`: no Alpine branch from v3.16 up ships 2.9, and 2.10+ advertises
# RSN replay counters this driver does not implement (docs/net.md). Build deps
# are --virtual and removed again, so none of them reach the image.
build_wpa() {
    tree=$1
    [ -f "$WPA_CONFIG" ] || die "missing $WPA_CONFIG (the 2.9 build config)"
    tarball=$BUILD/wpa_supplicant-$WPA_VER.tar.gz
    if [ -n "$WPA_SRC_TARBALL" ]; then
        tarball=$WPA_SRC_TARBALL
        [ -f "$tarball" ] || die "WPA_SRC_TARBALL=$tarball does not exist"
    elif [ ! -f "$tarball" ]; then
        log "downloading wpa_supplicant $WPA_VER"
        curl -fsSL -o "$tarball" "$WPA_URL" || die "could not download $WPA_URL"
    fi
    sum=$(sha256_of "$tarball")
    [ "$sum" = "$WPA_SHA256" ] || \
        die "$tarball sha256 is $sum, expected $WPA_SHA256 (do not bump WPA_VER without reading docs/net.md)"
    log "building wpa_supplicant $WPA_VER from source in the chroot (qemu, several minutes)"
    mkdir -p "$tree/tmp/wpa-build"
    cp "$tarball" "$tree/tmp/wpa-build/wpa.tar.gz"
    cp "$WPA_CONFIG" "$tree/tmp/wpa-build/wpa.config"
    chroot "$tree" /usr/bin/qemu-arm-static /bin/sh -c '
        set -e
        cd /tmp/wpa-build
        apk add --no-cache --virtual .wpa-build build-base linux-headers libnl3-dev >/dev/null
        tar -xzf wpa.tar.gz
        cd "wpa_supplicant-$1/wpa_supplicant"
        cp /tmp/wpa-build/wpa.config .config
        jobs=$(grep -c ^processor /proc/cpuinfo 2>/dev/null || echo 2)
        # 2.9 does not pass -DCONFIG_CRYPTO_INTERNAL through from .config, and
        # without it sha1-internal.c compiles to nothing (rootfs/wpa-2.9.config).
        make wpa_supplicant -j"$jobs" EXTRA_CFLAGS=-DCONFIG_CRYPTO_INTERNAL
        mkdir -p /usr/sbin
        cp wpa_supplicant /usr/sbin/wpa_supplicant
        chmod 755 /usr/sbin/wpa_supplicant
    ' vela "$WPA_VER" || die "wpa_supplicant $WPA_VER did not build (output above)"
    # Run the artifact before it ships. This assertion is the whole point: a
    # silent 2.10 substitution is the failure the project cannot afford.
    built=$(chroot "$tree" /usr/bin/qemu-arm-static /usr/sbin/wpa_supplicant -v 2>&1 | head -1)
    case "$built" in
        *"v$WPA_VER"*) log "wpa_supplicant check: $built" ;;
        *) die "built supplicant reports '$built', expected v$WPA_VER — do not ship 2.10 (docs/net.md)" ;;
    esac
    chroot "$tree" /usr/bin/qemu-arm-static /bin/sh -c \
        'apk del .wpa-build >/dev/null 2>&1 || true; rm -rf /tmp/wpa-build' \
        || log "warning: could not remove the wpa build tree (not fatal)"
}

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
    if [ "$BUILD_WPA" = "1" ]; then
        build_wpa "$TREE"
    else
        log "note: --no-wpa: this image has no supplicant, so no Wi-Fi"
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
    rm -f "$TREE"/etc/init.d/vela-*
    # ...and the runlevel links with them, or they point at nothing.
    rm -f "$TREE"/etc/runlevels/default/vela-*
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
if [ ! -e "$TREE/usr/sbin/velad" ] && [ ! -e "$TREE/bin/velad" ]; then
    log "note: no velad in the tree yet (P4 not built); init will report it and"
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
