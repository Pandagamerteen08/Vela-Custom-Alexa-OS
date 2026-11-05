# Vela build container.
#
# Why this exists: the kernel and rootfs steps need Linux semantics (chroot,
# QEMU user emulation, a cross toolchain, mksquashfs). Those do not exist on
# Windows or macOS, and "install WSL" is not a build instruction. This image is
# the one environment where `make` does the same thing on every host:
#
#   docker build -t vela-build .
#   docker run --rm --privileged -v "$PWD:/work" -w /work vela-build \
#       bash -lc 'tools/mkrootfs.sh && kernel/build.sh'
#
# or, from any OS, with Python as the only requirement:
#
#   python tools/vela.py docker rootfs
#
# Two stages because the aarch64 busybox for the initramfs is trivial to obtain
# with `apk` on Alpine and painful to extract by hand anywhere else.

FROM alpine:3.20 AS busybox-aarch64
# --root/--arch installs packages without running their triggers, which is what
# we want: busybox-static has none, and there is no aarch64 kernel here.
RUN apk add --no-cache --root /bb --arch aarch64 \
        --repository https://dl-cdn.alpinelinux.org/alpine/v3.20/main \
        busybox-static \
 && /bb/bin/busybox | head -1

FROM debian:bullseye-slim

ARG DEBIAN_FRONTEND=noninteractive

# bullseye is not an arbitrary choice: its gcc-aarch64-linux-gnu is GCC 10, and
# this is a Linux 4.9 tree. GCC 12+ (bookworm, trixie) rejects constructs that
# 4.9 and the MediaTek code use, so a newer base image costs an hour of
# confusing errors rather than saving anything.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash build-essential ca-certificates curl \
        bc bison flex libssl-dev libelf-dev \
        gcc-aarch64-linux-gnu binutils-aarch64-linux-gnu \
        qemu-user-static binfmt-support \
        squashfs-tools cpio xz-utils gzip file \
        device-tree-compiler \
        git python3 python3-yaml \
        vim less \
 && rm -rf /var/lib/apt/lists/*

# the initramfs must be aarch64 (the kernel is), while the rootfs is armv7
COPY --from=busybox-aarch64 /bb/bin/busybox /opt/busybox-aarch64
ENV BUSYBOX=/opt/busybox-aarch64

# Make the host's arm binfmt registration available to chroot; without it
# tools/mkrootfs.sh still runs, but apk's armv7 triggers cannot execute.
RUN dpkg --add-architecture armhf || true

WORKDIR /work
ENV CROSS_COMPILE=aarch64-linux-gnu- \
    PYTHONUNBUFFERED=1 \
    SOURCE_DATE_EPOCH=1700000000

# A default command that proves the image is usable rather than dropping into a
# root shell that looks identical to a broken one.
CMD ["bash", "-lc", "python3 tools/vela.py test"]
