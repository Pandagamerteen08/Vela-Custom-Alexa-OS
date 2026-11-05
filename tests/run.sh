#!/bin/sh
# Run every check that does not need the hardware.
#
#   ./tests/run.sh              everything
#   ./tests/run.sh tools        userspace tooling (boot image, DTB, cpio, kconfig)
#   ./tests/run.sh slots        the A/B state machine
#   ./tests/run.sh rootfs       the rootfs packer and package contract
#
# Exits non-zero if anything fails, so it can gate a build.
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT" || exit 1
WHAT=${1:-all}
RC=0

# python3 is not called python3 on every host (Windows, some macOS setups).
if [ -z "${PY:-}" ]; then
    if command -v python3 >/dev/null 2>&1; then PY=python3
    elif command -v python >/dev/null 2>&1; then PY=python
    else echo "no python3/python on PATH" >&2; exit 1; fi
fi

if command -v dtc >/dev/null 2>&1; then
    echo "== dtc available: $(dtc --version) (cross-checks the FDT patcher)"
else
    echo "== dtc not found; FDT tests use the in-tree round-trip plus tests/data/real.dtb"
fi

run_tools() {
    echo "== python unit tests"
    if "$PY" -m unittest discover -s tests -p 'test_*.py' -v; then
        echo "   tools: PASS"
    else
        echo "   tools: FAIL"; RC=1
    fi
}

run_slots() {
    echo "== slotctl state machine"
    if sh tests/slotctl.sh; then
        echo "   slotctl: PASS"
    else
        echo "   slotctl: FAIL"; RC=1
    fi
}

run_rootfs() {
    echo "== rootfs packer + package contract"
    if sh tests/mkrootfs.sh; then
        echo "   rootfs: PASS"
    else
        echo "   rootfs: FAIL"; RC=1
    fi
}

case "$WHAT" in
    all)    run_tools; run_slots; run_rootfs ;;
    tools)  run_tools ;;
    slots)  run_slots ;;
    rootfs) run_rootfs ;;
    *) echo "usage: $0 [all|tools|slots|rootfs]" >&2; exit 2 ;;
esac

if [ "$RC" -eq 0 ]; then
    echo
    echo "ALL CHECKS PASSED"
else
    echo
    echo "FAILURES PRESENT"
fi
exit "$RC"
