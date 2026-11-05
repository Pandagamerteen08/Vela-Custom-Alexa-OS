# Vela — build entry points.
#
# Every number in docs/ is reproducible with one command, and every gate fails
# the build rather than the device.
#
#   make test        host-only checks (tools + slot state machine + packer)
#   make check       toolchain and source sanity, no compilation
#   make boot        kernel + initramfs + boot.img
#   make boot-dev    the debuggable kernel (flash this when the panel is dark)
#   make trim        aggressive size trim (risky; boot-test it)
#   make sizes       what the artifacts actually cost
#   make release     package dist/ for a GitHub release
#   make doctor      what this machine can build
#
# Nothing here flashes anything. Flashing is tools/deploy.sh, dry-run by default.
#
# Portability: GNU make is assumed (it ships with macOS, and comes with Git Bash
# or Chocolatey on Windows). The recursive `python tools/vela.py` calls work on
# any OS; the shell-script targets need a POSIX shell, which is why `make test`
# on Windows wants Git Bash while `python tools/vela.py test` does not.

ROOT      := $(CURDIR)
BUILD     := $(ROOT)/build
KERNEL_OUT:= $(BUILD)/kernel

# nproc is GNU, getconf is POSIX, sysctl is macOS.
CPUS := $(shell nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || \
                sysctl -n hw.ncpu 2>/dev/null || echo 4)
JOBS ?= $(CPUS)

# python3 is not called python3 everywhere (Windows, some macOS setups).
PY := $(shell command -v python3 2>/dev/null || command -v python 2>/dev/null)

export KERNEL_SRC ?= $(ROOT)/kernel/src
export OUT        ?= $(KERNEL_OUT)

BOOT_IMG  := $(BUILD)/boot.img
INITRAMFS := $(BUILD)/initramfs.cpio.gz

.PHONY: all test check boot boot-dev trim sizes clean help toolchain \
        rootfs rootfs-dry doctor release docker-test

all: test

help:
	@sed -n '2,20p' Makefile

test:
	@sh tests/run.sh

doctor:
	@$(PY) tools/vela.py doctor

check:
	@bash kernel/build.sh --check
	@bash tools/mkinitramfs.sh --dry-run

toolchain:
	@echo "python:    $(PY)"
	@echo "cross gcc: $(shell command -v aarch64-linux-gnu-gcc || echo MISSING)"
	@echo "dtc:       $(shell command -v dtc || echo 'optional, tests only')"
	@echo "mksquashfs:$(shell command -v mksquashfs || echo MISSING)"

boot:
	@mkdir -p $(BUILD)
	@bash kernel/build.sh

boot-dev:
	@mkdir -p $(BUILD)
	@bash kernel/build.sh --dev

trim:
	@mkdir -p $(BUILD)
	@bash kernel/build.sh --trim

$(INITRAMFS):
	@mkdir -p $(BUILD)
	@bash tools/mkinitramfs.sh --out $(INITRAMFS)

initramfs: $(INITRAMFS)

rootfs:
	@bash tools/mkrootfs.sh

rootfs-dry:
	@bash tools/mkrootfs.sh --dry-run

sizes:
	@$(PY) tools/report-sizes.py $(BUILD)

release:
	@$(PY) tools/vela.py release

# The Linux-only steps without a Linux host.
docker-test:
	@$(PY) tools/vela.py docker test

clean:
	@$(PY) -c "import shutil,os; [shutil.rmtree(p, True) for p in ('build','dist')]"
	@echo "removed build/ and dist/ (kernel source in kernel/src is kept)"
