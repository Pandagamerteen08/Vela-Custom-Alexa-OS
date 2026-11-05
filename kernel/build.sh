#!/bin/bash
# Build the cronos kernel and boot.img.
#
# This wraps the one kernel tree that actually works on this board
# (amazon-oss/android_kernel_amazon_kernel_mt8163, branch cronos/lineage-18.1,
# 4.9.337) and produces a flashable boot.img with:
#   * our config fragment applied and asserted
#   * the DTB patched (amzn,mic-downmix removed)
#   * the initramfs from tools/mkinitramfs.sh
#
# Usage:
#   kernel/build.sh --check            # toolchain and source sanity only
#   kernel/build.sh                    # prod kernel + boot.img
#   kernel/build.sh --dev              # dev kernel + boot.img.dev
#   kernel/build.sh --trim             # aggressive size trim (risky, see fragment)
#   kernel/build.sh --defconfig        # dump the vendor defconfig name
#
# Environment:
#   KERNEL_SRC   default ./kernel/src
#   OUT          default ./build/kernel
#   CROSS_COMPILE default aarch64-linux-gnu-
#   JOBS         default: number of CPUs
#   USE_CLANG=1  build with AOSP clang instead of GCC
#
# Toolchain warning worth taking seriously: this is Linux 4.9. GCC >= 12 rejects
# several constructs in 4.9 and the MTK tree in particular; GCC 6-10 or AOSP
# clang 10-12 are the known-good choices. The script warns rather than guesses.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
KERNEL_SRC=${KERNEL_SRC:-$ROOT/kernel/src}
OUT=${OUT:-$ROOT/build/kernel}
CROSS_COMPILE=${CROSS_COMPILE:-aarch64-linux-gnu-}
DEFCONFIG=${DEFCONFIG:-cronos_defconfig}

# --- portability ----------------------------------------------------------
# This is expected to run on Debian/Ubuntu, Alpine, Fedora, a Raspberry Pi, and
# on macOS or Git Bash for the non-kernel parts. None of nproc, stat -c or the
# name python3 exist everywhere, so probe once instead of assuming.
if [ -z "${PY:-}" ]; then
    if command -v python3 >/dev/null 2>&1; then PY=python3
    elif command -v python >/dev/null 2>&1; then PY=python
    else printf '[build] error: no python3/python on PATH\n' >&2; exit 1; fi
fi
_cpus() {
    nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || \
        sysctl -n hw.ncpu 2>/dev/null || echo 4
}
fsize() {
    stat -c%s "$1" 2>/dev/null || stat -f%z "$1" 2>/dev/null || \
        wc -c <"$1" | tr -d ' '
}
JOBS=${JOBS:-$(_cpus)}
REPO=${KERNEL_REPO:-https://github.com/amazon-oss/android_kernel_amazon_mtk8163}
BRANCH=${KERNEL_BRANCH:-cronos/lineage-18.1}

TRIM=0
DEV=0
CHECK_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --trim) TRIM=1 ;;
        --dev) DEV=1 ;;
        --check) CHECK_ONLY=1 ;;
        --defconfig)
            echo "defconfig candidates in the tree (grep arch/arm64/configs):"
            ls "$KERNEL_SRC/arch/arm64/configs" 2>/dev/null || \
                echo "  source not fetched yet; run without --defconfig once"
            exit 0 ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

log() { printf '\033[1m[build]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[build] error:\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1. toolchain ---------------------------------------------------------
if [ "${USE_CLANG:-0}" = "1" ]; then
    command -v clang >/dev/null || die "USE_CLANG=1 but clang is not on PATH"
    MAKE_VARS=(CC=clang CLANG_TRIPLE=aarch64-linux-gnu- CROSS_COMPILE=aarch64-linux-gnu-)
    log "using clang $(clang --version | head -1)"
else
    CC_BIN="${CROSS_COMPILE}gcc"
    command -v "$CC_BIN" >/dev/null || die \
        "$CC_BIN not found. Install an aarch64 cross compiler (gcc-aarch64-linux-gnu) \
or set CROSS_COMPILE. For this 4.9 tree prefer GCC 6-10."
    CC_MAJOR=$("$CC_BIN" -dumpversion | cut -d. -f1)
    if [ "$CC_MAJOR" -ge 12 ]; then
        log "WARNING: $CC_BIN is GCC $CC_MAJOR; Linux 4.9 usually fails to build with >= 12."
        log "         Use gcc-10 or AOSP clang, or expect unrelated compile errors."
    fi
    MAKE_VARS=(CROSS_COMPILE="$CROSS_COMPILE")
fi
for tool in "${CROSS_COMPILE}objcopy" "${CROSS_COMPILE}ld"; do
    command -v "$tool" >/dev/null || log "WARNING: $tool not found"
done

# --- 2. source ------------------------------------------------------------
if [ ! -d "$KERNEL_SRC/.git" ]; then
    log "cloning $REPO ($BRANCH) into $KERNEL_SRC"
    log "note: this tree is large (~2 GB with history); --depth 1 keeps it usable"
    mkdir -p "$(dirname "$KERNEL_SRC")"
    git clone --depth 1 --branch "$BRANCH" "$REPO" "$KERNEL_SRC" || die \
        "clone failed. Check the branch name: the tree is
        amazon-oss/android_kernel_amazon_mt8163 and the branch is cronos/lineage-18.1.
        If the repository was renamed, set KERNEL_REPO/KERNEL_BRANCH."
fi
cd "$KERNEL_SRC"

if [ ! -f arch/arm64/configs/$DEFCONFIG ]; then
    log "defconfig '$DEFCONFIG' not present; available:"
    ls arch/arm64/configs | sed 's/^/    /' || true
    die "set DEFCONFIG=<name from the list above>"
fi

if [ "$CHECK_ONLY" = "1" ]; then
    log "check mode OK: source present, defconfig '$DEFCONFIG' found"
    exit 0
fi

# --- 3. configure ---------------------------------------------------------
mkdir -p "$OUT"
log "defconfig $DEFCONFIG"
make -s ARCH=arm64 O="$OUT" "${MAKE_VARS[@]}" "$DEFCONFIG"

FRAGS=(--fragment "$ROOT/kernel/cronos.fragment")
[ "$TRIM" = "1" ] && FRAGS+=(--fragment "$ROOT/kernel/cronos-trim.fragment")
[ "$DEV" = "1" ] && FRAGS+=(--fragment "$ROOT/kernel/cronos-dev.fragment")

log "applying fragments: ${FRAGS[*]}"
"$PY" "$ROOT/tools/kconfig.py" apply --config "$OUT/.config" "${FRAGS[@]}" -o "$OUT/.config.new"
mv "$OUT/.config.new" "$OUT/.config"

log "olddefconfig (may resolve dependencies; assertions run after)"
make -s ARCH=arm64 O="$OUT" "${MAKE_VARS[@]}" olddefconfig

log "asserting required symbols"
if ! "$PY" "$ROOT/tools/kconfig.py" check --config "$OUT/.config" \
        --assert "$ROOT/kernel/cronos.assert"; then
    die "config assertions failed. A symbol you asked for was overridden by a
     dependency. Inspect with: tools/kconfig.py diff --config $OUT/.config --other <old>"
fi

# --- 4. build -------------------------------------------------------------
VERSION=$(git -C "$KERNEL_SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)
log "building Image.gz + dtbs + modules (jobs=$JOBS)"
make -j"$JOBS" ARCH=arm64 O="$OUT" "${MAKE_VARS[@]}" Image.gz dtbs modules

IMG="$OUT/arch/arm64/boot/Image.gz"
[ -f "$IMG" ] || die "no Image.gz produced"

# --- 5. DTB ---------------------------------------------------------------
DTB_SRC=""
for cand in "$OUT/arch/arm64/boot/dts/mediatek/mt8163-cronos.dtb" \
            "$OUT/arch/arm64/boot/dts/mt8163-cronos.dtb" \
            "$OUT/arch/arm64/boot/dts/mediatek/mt8163.dtb"; do
    [ -f "$cand" ] && { DTB_SRC="$cand"; break; }
done
if [ -z "$DTB_SRC" ]; then
    log "warning: no cronos DTB in the build output; using the stock DTB"
    log "         (extract it from a stock boot.img: tools/mkbootimg.py unpack)"
    DTB_SRC=$(ls "$ROOT"/dtb/mt8163*.dtb 2>/dev/null | head -1 || true)
    [ -n "$DTB_SRC" ] || die "no DTB found. Put the stock DTB in dtb/ or fix the DTS name."
fi

DTB_PATCHED="$OUT/mt8163-cronos-patched.dtb"
log "patching DTB: $(basename "$DTB_SRC")"
"$PY" "$ROOT/tools/patch-dtb.py" "$DTB_SRC" -o "$DTB_PATCHED" --no-mic-downmix

# --- 6. initramfs ---------------------------------------------------------
INITRAMFS="$ROOT/build/initramfs.cpio.gz"
if [ ! -f "$INITRAMFS" ]; then
    log "building initramfs (needs an aarch64 static busybox; see tools/mkinitramfs.sh)"
    "$ROOT/tools/mkinitramfs.sh" --out "$INITRAMFS" || die \
        "initramfs build failed. A boot.img without a ramdisk cannot mount the
     squashfs slot, so this is fatal rather than optional."
fi

# --- 7. boot.img ----------------------------------------------------------
KERNEL_FINAL="$OUT/Image.gz+dtb"
cat "$IMG" "$DTB_PATCHED" > "$KERNEL_FINAL"
log "appended DTB: $(fsize "$IMG") + $(fsize "$DTB_PATCHED") bytes"

CMDLINE_FILE="$ROOT/kernel/cmdline.txt"
if [ "$DEV" = "1" ]; then
    BOOT_OUT="$ROOT/build/boot.img.dev"
else
    BOOT_OUT="$ROOT/build/boot.img"
fi

"$PY" "$ROOT/tools/mkbootimg.py" pack \
    --kernel "$KERNEL_FINAL" \
    --ramdisk "$INITRAMFS" \
    --cmdline-file "$CMDLINE_FILE" \
    --name "cronos-min-$VERSION" \
    --out "$BOOT_OUT"

log "done: $BOOT_OUT (kernel $VERSION$([ "$TRIM" = 1 ] && echo ', trimmed')$([ "$DEV" = 1 ] && echo ', dev'))"
log "flash with:  tools/deploy.sh boot $BOOT_OUT   (dry-run by default)"
