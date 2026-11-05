# cronos-min — build entry points.
#
# The point of this file is that every number in docs/ can be reproduced with
# one command, and every gate fails the build rather than the device.
#
#   make test        host-only checks (tools + slot state machine)
#   make check       toolchain and source sanity, no compilation
#   make boot        kernel + initramfs + boot.img
#   make boot-dev    the debuggable kernel (flash this when the panel is dark)
#   make trim        aggressive size trim (risky; boot-test it)
#   make sizes       what the artifacts actually cost
#
# Nothing here flashes anything. Flashing is tools/deploy.sh, dry-run by default.

ROOT      := $(CURDIR)
BUILD     := $(ROOT)/build
KERNEL_OUT:= $(BUILD)/kernel
JOBS      ?= $(shell nproc 2>/dev/null || echo 4)

export KERNEL_SRC ?= $(ROOT)/kernel/src
export OUT        ?= $(KERNEL_OUT)

BOOT_IMG     := $(BUILD)/boot.img
BOOT_DEV_IMG := $(BUILD)/boot.img.dev
INITRAMFS    := $(BUILD)/initramfs.cpio.gz

.PHONY: all test check boot boot-dev trim sizes clean help toolchain rootfs rootfs-dry

all: test

help:
	@sed -n '1,16p' Makefile

test:
	@sh tests/run.sh

check:
	@bash kernel/build.sh --check
	@tools/mkinitramfs.sh --dry-run

toolchain:
	@echo "cross gcc: $(shell command -v aarch64-linux-gnu-gcc || echo MISSING)"
	@echo "python3:   $(shell python3 -V 2>&1)"
	@echo "dtc:       $(shell command -v dtc || echo 'not needed, tools/patch-dtb.py is self-contained')"

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
	@tools/mkinitramfs.sh --out $(INITRAMFS)

initramfs: $(INITRAMFS)

rootfs:
	@$(ROOT)/tools/mkrootfs.sh

rootfs-dry:
	@$(ROOT)/tools/mkrootfs.sh --dry-run

sizes:
	@python3 tools/report-sizes.py $(BUILD)

clean:
	@rm -rf $(BUILD)
	@echo "removed $(BUILD) (kernel source in kernel/src is kept; 'make distclean-src' removes it)"
