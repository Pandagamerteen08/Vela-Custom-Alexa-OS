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
# The aarch64 busybox for the initramfs is fetched as a pinned artifact rather
# than installed with `apk`. An earlier version ran `apk add --arch aarch64
# --initdb` in alpine:3.20 and every CI run failed with:
#
#   WARNING: fetching .../v3.20/main: UNTRUSTED signature
#   ERROR: unable to select packages: busybox-static (no such package)
#
# i.e. the image's keyring no longer matches the repository's signature, so no
# index was readable and the package "did not exist". A direct download with a
# SHA-256 check has no keyring, no apk, and no second base image to pull.

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

# Pinned: busybox-static 1.36.1-r31 from Alpine v3.20 main/aarch64.
ARG BUSYBOX_STATIC_URL=https://dl-cdn.alpinelinux.org/alpine/v3.20/main/aarch64/busybox-static-1.36.1-r31.apk
ARG BUSYBOX_STATIC_SHA256=1d8e7a7fc2ed69bdb8eb7be9d962489c21a0f75b72d8a325437c8a09d4cfac70

# The initramfs must be aarch64 (the kernel is), while the rootfs is armv7.
# The package ships bin/busybox.static; the build then checks the architecture
# instead of trusting the URL, so a wrong-arch download fails here rather than
# on the device.
RUN curl -fsSL "$BUSYBOX_STATIC_URL" -o /tmp/bb.apk \
 && echo "$BUSYBOX_STATIC_SHA256  /tmp/bb.apk" | sha256sum -c - \
 && tar -xzf /tmp/bb.apk -C /tmp \
 && install -m 0755 /tmp/bin/busybox.static /opt/busybox-aarch64 \
 && file /opt/busybox-aarch64 | grep -q 'ARM aarch64' \
 && rm -rf /tmp/bb.apk /tmp/bin

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
