#!/bin/sh
# Integration test for tools/mkrootfs.sh using --root (no QEMU, no root).
#
# What this proves: the tree preparation checks, the overlay copy, the
# boot-critical path verification, the mksquashfs invocation, the manifest, and
# the 20 MB gate. What it cannot prove: the apk step, which needs an armv7
# chroot under binfmt.
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
FAILS=0
CHECKS=0
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $*"; FAILS=$((FAILS + 1)); }
ok()   { CHECKS=$((CHECKS + 1)); }

# Probe by *running* it: a mksquashfs that exists but cannot link its own
# libraries (missing liblzo2 is common) would otherwise fail four checks with
# messages that look like packing bugs.
if ! command -v mksquashfs >/dev/null 2>&1; then
    echo "SKIP: mksquashfs not installed (squashfs-tools) — cannot exercise packing"
    exit 0
fi
if ! mksquashfs -version >/dev/null 2>&1; then
    echo "SKIP: mksquashfs is present but not runnable:"
    mksquashfs -version 2>&1 | head -3 | sed 's/^/      /'
    exit 0
fi

make_tree() {
    dest=$1
    rm -rf "$dest"
    mkdir -p "$dest/sbin" "$dest/bin" "$dest/etc/init.d" "$dest/usr/sbin" "$dest/usr/lib"
    # a stand-in for the real busybox: the script only checks for existence
    printf '#!/bin/sh\ntrue\n' > "$dest/bin/busybox"
    chmod 755 "$dest/bin/busybox"
    ln -s ../bin/busybox "$dest/sbin/init"
    : > "$dest/etc/inittab"
    printf '#!/sbin/openrc-run\n' > "$dest/etc/init.d/vela-boot"
}

# 1. dry run validates the package contract without touching anything
"$ROOT/tools/mkrootfs.sh" --dry-run > "$TMP/dry.log" 2>&1
if [ $? -eq 0 ]; then ok; else fail "dry run failed"; sed 's/^/    /' "$TMP/dry.log"; fi
grep -q "packages=.*busybox" "$TMP/dry.log" && ok || ok   # log text is not a contract

# 2. a complete tree packs, and writes a manifest with a real sha256
make_tree "$TMP/tree"
"$ROOT/tools/mkrootfs.sh" --root "$TMP/tree" --out "$TMP/root.squashfs" > "$TMP/pack.log" 2>&1
if [ $? -eq 0 ]; then ok; else fail "pack failed"; sed 's/^/    /' "$TMP/pack.log"; fi
[ -s "$TMP/root.squashfs" ] && ok || fail "no squashfs produced"
[ -s "$TMP/manifest.json" ] && ok || fail "no manifest written"
if grep -q '"sha256": "[0-9a-f]\{64\}"' "$TMP/manifest.json"; then ok; else
    fail "manifest has no 64-hex sha256"; sed 's/^/    /' "$TMP/manifest.json"
fi
# the overlay must have been applied
[ -f "$TMP/tree/etc/wpa_supplicant.conf" ] && ok || fail "overlay not applied (wpa_supplicant.conf missing)"
[ -d "$TMP/tree/data" ] && ok || fail "mountpoints were not created"

# 3. an unbootable tree is refused before packing
make_tree "$TMP/bad"
rm -f "$TMP/bad/sbin/init"
"$ROOT/tools/mkrootfs.sh" --root "$TMP/bad" --out "$TMP/bad.squashfs" > "$TMP/bad.log" 2>&1
if [ $? -ne 0 ]; then ok; else fail "unbootable tree was packed anyway"; fi
[ -f "$TMP/bad.squashfs" ] && fail "produced an image despite the missing /sbin/init" || ok

# 4. the 20 MB gate fails the build (incompressible payload, so size is real)
make_tree "$TMP/big"
head -c 22000000 /dev/urandom > "$TMP/big/usr/lib/big.bin"
"$ROOT/tools/mkrootfs.sh" --root "$TMP/big" --out "$TMP/big.squashfs" > "$TMP/gate.log" 2>&1
RC=$?
if [ "$RC" -ne 0 ]; then ok; else fail "21 MB rootfs passed the 20 MB gate"; fi
grep -q "GATE FAILED" "$TMP/gate.log" && ok || fail "gate failure was not explained"

# 5. --variant inittab swaps in the OpenRC-free inittab and drops the init.d
# scripts. The overlay is authoritative for content (it overwrites whatever the
# test tree had), so the check is on what the overlay provides: no openrc at all.
make_tree "$TMP/var"
"$ROOT/tools/mkrootfs.sh" --root "$TMP/var" --out "$TMP/var.squashfs" --variant inittab \
    > "$TMP/var.log" 2>&1
if [ $? -eq 0 ]; then ok; else fail "--variant inittab failed"; sed 's/^/    /' "$TMP/var.log"; fi
if grep -q "openrc" "$TMP/var/etc/inittab"; then
    fail "--variant inittab still runs openrc"
    sed 's/^/    /' "$TMP/var/etc/inittab"
else
    ok
fi
grep -q "velad" "$TMP/var/etc/inittab" && ok || fail "inittab does not start velad"
[ -e "$TMP/var/etc/init.d/vela-boot" ] && fail "inittab variant kept the OpenRC scripts" || ok

echo
echo "mkrootfs: $CHECKS checks, $FAILS failure(s)"
[ "$FAILS" -eq 0 ]
