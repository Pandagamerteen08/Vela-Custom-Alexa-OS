#!/bin/sh
# Integration test for src/slotctl/slotctl.c — the A/B boot state machine.
#
# Builds a host binary, then walks the exact sequence the device takes:
#   fresh -> trials -> revert -> re-arm -> commit -> corruption -> double failure.
# If this passes on x86 it is still not proof (endianness is fixed, but the
# on-media behaviour is not), yet every state transition is exercised here.
set -u

# slotctl is device code: it uses fsync and O_DIRECTORY, the Linux dirfd API that
# MSYS/MinGW does not provide. The A/B state machine is exercised on Linux and
# macOS; on Windows there is nothing to compile it against, and reporting a
# missing system header as a failed test is worse than saying so.
case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*)
        echo "SKIP: slotctl needs POSIX fsync/O_DIRECTORY, which Windows does not have"
        echo "      (the A/B state machine is exercised on Linux and macOS)"
        exit 0 ;;
esac

ROOT=$(cd "$(dirname "$0")/.." && pwd)
BIN=${BIN:-$ROOT/build/slotctl}
FAILS=0
CHECKS=0
# TMPDIR is unset on some hosts; /tmp does not exist on native Windows.
TMPBASE=${TMPDIR:-/tmp}
OUT_FILE="$TMPBASE/slotctl.out"
ERR_FILE="$TMPBASE/slotctl.err"

fail() { echo "FAIL: $*"; FAILS=$((FAILS + 1)); }
ok()   { CHECKS=$((CHECKS + 1)); }

# NOTE: no `local` in POSIX sh, so every helper variable is namespaced. An
# earlier version used `want`, which silently clobbered the caller's loop
# variable and made three iterations check the same expectation.
expect_rc() {
    rc_want=$1; shift
    rc_desc=$1; shift
    "$@" >"$OUT_FILE" 2>"$ERR_FILE"
    rc_got=$?
    if [ "$rc_got" -eq "$rc_want" ]; then
        ok
    else
        fail "$rc_desc: exit $rc_got, wanted $rc_want"; sed 's/^/    /' "$ERR_FILE"
    fi
}

expect_out() {
    out_desc=$1
    out_needle=$2
    if grep -q -- "$out_needle" "$OUT_FILE"; then
        ok
    else
        fail "$out_desc: output missing '$out_needle'"; sed 's/^/    /' "$OUT_FILE"
    fi
}

mkdir -p "$ROOT/build"
if [ ! -x "$BIN" ] || [ "$ROOT/src/slotctl/slotctl.c" -nt "$BIN" ]; then
    echo "building $BIN"
    if ! cc -O2 -std=c11 -Wall -Wextra -Werror -static -o "$BIN" \
            "$ROOT/src/slotctl/slotctl.c" 2>"$ERR_FILE"; then
        echo "  static link failed, retrying dynamically"
        if ! cc -O2 -std=c11 -Wall -Wextra -Werror -o "$BIN" \
                "$ROOT/src/slotctl/slotctl.c" 2>"$ERR_FILE"; then
            echo "FAIL: cannot compile slotctl.c"; cat "$ERR_FILE"; exit 1
        fi
    fi
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
export SLOT_PTR="$TMP/slot.ptr"

# 1. crc/layout self test
expect_rc 0 "selftest" "$BIN" selftest
expect_out "selftest" "selftest: ok"

# 2. no record yet -> exit 2 (initramfs treats this as "fresh install")
expect_rc 2 "boot with no record" "$BIN" boot
expect_rc 2 "get with no record" "$BIN" get

# 3. init -> slot A committed
expect_rc 0 "init" "$BIN" init
expect_rc 0 "get after init" "$BIN" get
expect_out "get after init" "active=A trials=255"

# 4. arming B gives exactly 3 trial boots
expect_rc 0 "pending" "$BIN" pending
expect_rc 0 "get after pending" "$BIN" get
expect_out "pending result" "active=B trials=3"
for want in 2 1 0; do
    expect_rc 0 "trial boot (expect $want left)" "$BIN" boot
    expect_rc 0 "get during trials" "$BIN" get
    expect_out "trials decrement to $want" "trials=$want"
done

# 5. fourth boot: B never committed -> revert to A, B marked bad
expect_rc 0 "revert boot" "$BIN" boot
expect_rc 0 "get after revert" "$BIN" get
expect_out "reverted to A" "active=A trials=255"
expect_out "B flagged bad" "flags=2"

# 6. re-arming B clears the bad flag and gives it a fresh 3 trials
expect_rc 0 "re-arm pending" "$BIN" pending
expect_rc 0 "get after re-arm" "$BIN" get
expect_out "re-armed B" "active=B trials=3"
expect_out "B flag cleared" "flags=0"

# 7. an explicit commit makes it permanent
expect_rc 0 "commit" "$BIN" commit
expect_rc 0 "get after commit" "$BIN" get
expect_out "committed B" "active=B trials=255"

# 8. corrupt copy 0 only: copy 1 must still be usable
cp "$TMP/slot.ptr" "$TMP/pristine"
printf 'XXXX' | dd of="$TMP/slot.ptr" bs=1 count=4 conv=notrunc 2>/dev/null
expect_rc 0 "get with copy 0 corrupt" "$BIN" get
expect_out "fell back to copy 1" "valid=0,1"
cp "$TMP/pristine" "$TMP/slot.ptr"

# 9. corrupt *both* copies -> exit 2
printf 'XXXX' | dd of="$TMP/slot.ptr" bs=1 count=4 conv=notrunc 2>/dev/null
printf 'XXXX' | dd of="$TMP/slot.ptr" bs=1 seek=2048 count=4 conv=notrunc 2>/dev/null
expect_rc 2 "both copies corrupt" "$BIN" get

# 10. a record truncated to 64 bytes keeps only copy 0; wipe that and it's gone
dd if=/dev/zero of="$TMP/slot.ptr" bs=1 count=0 seek=64 2>/dev/null
printf 'XXXX' | dd of="$TMP/slot.ptr" bs=1 count=4 conv=notrunc 2>/dev/null
expect_rc 2 "short file, copy 0 corrupt" "$BIN" get

# 11. both slots bad -> exit 3 (rescue), the state that must never loop.
# Note: pending always arms the *inactive* slot, so a committed slot can only be
# marked bad by an explicit failure — which is exactly what `fail` is for.
rm -f "$TMP/slot.ptr"
"$BIN" init >/dev/null                 # A committed
"$BIN" fail >/dev/null 2>&1            # A bad -> B
"$BIN" fail >/dev/null 2>&1            # B bad -> A (both flagged)
expect_rc 3 "both slots bad" "$BIN" boot
expect_rc 3 "both slots bad (fail)" "$BIN" fail

# 12. SLOT_PTR is honoured, and -f overrides it
expect_rc 0 "-f override init" "$BIN" -f "$TMP/other.ptr" init
test -s "$TMP/other.ptr" && ok || fail "-f did not create the file"

echo
echo "slotctl: $CHECKS checks, $FAILS failure(s)"
[ "$FAILS" -eq 0 ]
