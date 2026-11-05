# Getting a working build host

The one page to read with a fresh machine in front of you. Everything here is
either a package from your distro or a download from the vendor's own site — no
third-party scripts, no `curl | sh`.

`python3 tools/vela.py doctor` prints the same information for the machine you
are actually sitting at, including the single `apt` (or `dnf`, or `pacman`, or
`brew`) line for whatever is missing. This page is the copy-paste version with
the links.

---

## Zorin OS, Ubuntu, Debian, Pop!_OS, Mint

Zorin OS 17 is Ubuntu 22.04 (`jammy`) under the desktop; Zorin OS 18 is Ubuntu
24.04 (`noble`). Both ship every package below, so the same block works on both
and on plain Ubuntu.

```sh
sudo apt update
sudo apt install -y build-essential git python3 bc bison flex \
    libssl-dev libelf-dev cpio \
    gcc-10-aarch64-linux-gnu binutils-aarch64-linux-gnu \
    squashfs-tools qemu-user-static binfmt-support device-tree-compiler \
    adb fastboot android-sdk-platform-tools-common
```

Then, in the repository:

```sh
git clone https://github.com/Pandagamerteen08/Bloom-Alexa-OS Vela
cd Vela
python3 tools/vela.py doctor      # what this machine can now build
make test                         # every host check
```

`make test` on a complete host ends with:

```
== python unit tests (any OS)
Ran 54 tests ... OK
   tools: PASS
== slotctl state machine
slotctl: 38 checks, 0 failure(s)
   slotctl: PASS
== rootfs packer + package contract
mkrootfs: 16 checks, 0 failure(s)
   rootfs: PASS

ALL CHECKS PASSED
```

If any of those three lines says `SKIP` instead of `PASS`, the reason is printed
next to it — a skip is not a pass, and this build host is the wrong place to
discover it (see [Troubleshooting](#troubleshooting)).

### What each package is for

| Package | Unlocks | If missing |
|---|---|---|
| `build-essential` | `make`, `cc` — compiles `slotctl` for the 38 A/B checks | those checks report `SKIP` |
| `git` | cloning the 4.9 kernel tree | `kernel/build.sh` stops |
| `python3` | every tool in `tools/` (3.8+, no third-party modules) | nothing runs |
| `bc`, `bison`, `flex` | the kernel's Kconfig and its parsers | kernel build fails at `prepare` |
| `libssl-dev` | `scripts/sign-file`, module signing | kernel build fails during `modules` |
| `libelf-dev` | `objtool` when debug info is on | `--dev` kernel build fails |
| `cpio` | initramfs generation | `mkinitramfs.sh` refuses |
| `gcc-10-aarch64-linux-gnu` | the arm64 kernel — **GCC 10, not the default 13** | `build.sh` warns, then dies in a wall of errors |
| `binutils-aarch64-linux-gnu` | `aarch64-linux-gnu-ld`, `-objcopy` | link fails |
| `squashfs-tools` | `mksquashfs` — packs the rootfs, enforces the 20 MB gate | `mkrootfs.sh` refuses; packer checks `SKIP` |
| `qemu-user-static` + `binfmt-support` | running armv7 `apk` and its triggers inside a chroot | the rootfs builds but package triggers silently do not run |
| `device-tree-compiler` | `dtc`, to cross-check the DTB patcher (optional) | one test is skipped |
| `adb` | pushing slot images, `deploy.sh` | nothing reaches the device |
| `fastboot` | flashing `boot.img` | `deploy.sh` refuses |
| `android-sdk-platform-tools-common` | udev rules, so `adb devices` sees the Echo as your user | `adb` needs `sudo`, which breaks `adb shell` redirection |

### The two version traps

**1. The default cross compiler is too new.** Ubuntu 22.04 ships
`gcc-aarch64-linux-gnu` as GCC 11, and 24.04 as GCC 13/14. This is a Linux 4.9
tree that has never seen either. Install `gcc-10-aarch64-linux-gnu` (in
`universe` — see [Troubleshooting](#troubleshooting)) and select it, because the
default name still points at the wrong compiler:

```sh
CROSS_COMPILE=aarch64-linux-gnu-10- kernel/build.sh
```

**2. armv7 userspace, aarch64 initramfs.** The kernel is 64-bit, so the
initramfs `busybox` must be aarch64; the rootfs is armv7. A 32-bit busybox in the
initramfs fails with `Exec format error` before any console exists, which is the
least diagnosable failure this board has. `mkinitramfs.sh` refuses the wrong
architecture by name, so this trap is closed — but do not "fix" the refusal.

### Verify the emulation actually registered

```sh
ls /proc/sys/fs/binfmt_misc/ | grep -i -e qemu -e arm
# expect: qemu-arm   qemu-aarch64
```

Nothing listed means `apk`'s armv7 triggers could not execute. On Ubuntu that is
almost always a container without `--privileged`, or a host that needs:

```sh
sudo systemctl restart systemd-binfmt
```

### The one file you must fetch by hand

The initramfs needs a **static aarch64 busybox**. Alpine distributes exactly one,
as a `.apk`, and `apk` is not a tool you have on Ubuntu. Three ways, best first:

```sh
# 1. let the pinned container do it (it downloads and verifies for you)
python3 tools/vela.py docker kernel

# 2. fetch the current package and unpack the binary
BASE=https://dl-cdn.alpinelinux.org/alpine/v3.20/main/aarch64
PKG=$(curl -fsSL "$BASE/" | grep -o 'busybox-static-[0-9][^"]*\.apk' | head -1)
curl -fsSLO "$BASE/$PKG"
tar -xf "$PKG" bin/busybox        # add --ignore-zeros if tar stops at the signature
file bin/busybox                  # must say: ELF 64-bit ... ARM aarch64

# 3. copy it off any arm64 Alpine you already have
scp box:/bin/busybox ./busybox
```

Then: `tools/mkinitramfs.sh --busybox ./bin/busybox`.

`strip` it if you care about the 15.5 MB gate: `aarch64-linux-gnu-strip bin/busybox`
takes it from ~2 MB to ~1 MB, and `kernel/build.sh` measures the result.

---

## Fastest path to something you can see

Once the block above is installed, in this order:

```sh
make test                      # ~5 s, no root, no device, no kernel tree

kernel/build.sh --check        # toolchain + source sanity, no clone
CROSS_COMPILE=aarch64-linux-gnu-10- kernel/build.sh --dev
                               # ~15-40 min, ~2 GB clone, ~6 GB disk
                               # --dev turns on fbcon + serial + kallsyms,
                               # so the panel prints text instead of staying black

sudo tools/mkrootfs.sh         # ~1 min after the armv7 minirootfs downloads

make sizes                     # fails rather than letting you flash an oversized image
tools/deploy.sh boot build/boot.img          # dry run, prints the commands
```

`--dev` is the deliberate first flash: a black panel is indistinguishable from a
dead kernel, and the dev kernel costs 3–4 MB to remove that ambiguity. The
production kernel comes later, once something works.

Flashing, backing up the stock images and rollback: [docs/install.md](install.md)
step 5 onward. Nothing here writes to the device on its own.

---

## Other hosts

| Host | Host checks | Kernel + rootfs | Notes |
|---|---|---|---|
| Zorin / Ubuntu / Debian 64-bit | native | **native** | this page |
| Fedora | native | native | `dnf install @development-tools ...` — see [platforms.md](platforms.md) |
| Arch / Manjaro | native | native | `pacman -S base-devel ...` — see [platforms.md](platforms.md) |
| macOS (Intel or Apple silicon) | native | container only | case-insensitive APFS breaks the kernel build |
| Windows 10/11 | native, with Git Bash | container only, or WSL2 | line endings: see [platforms.md](platforms.md) |
| Raspberry Pi, 64-bit OS | native | native | slow, and `-j$(nproc)` OOMs the link on 2 GB |

Per-OS detail and the footguns: [docs/platforms.md](platforms.md). The container
that pins GCC 10 and QEMU for the non-Linux hosts: `./Dockerfile`, driven by
`python3 tools/vela.py docker <rootfs|kernel|kernel-check|test|shell>`.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `E: Unable to locate package gcc-10-aarch64-linux-gnu` | `universe` is disabled | `sudo add-apt-repository universe && sudo apt update` |
| `cc1: error: ... unknown` walls of errors from 4.9 | GCC ≥ 12 | use `CROSS_COMPILE=aarch64-linux-gnu-10-` |
| `slotctl: SKIP` on `make test` | no C compiler | `build-essential` |
| `rootfs: SKIP` | no `mksquashfs` | `squashfs-tools` |
| `apk: Exec format error` in the chroot | arm binfmt not registered | `qemu-user-static` + `systemd-binfmt`; in a container, `--privileged` |
| `bash\r: No such file or directory` | CRLF checkout | `.gitattributes` pins LF; `git rm --cached -r . && git reset --hard` |
| `adb devices` shows nothing / `no permissions` | udev rules or group | `android-sdk-platform-tools-common`, then re-plug and `sudo usermod -aG plugdev $USER` |
| `No space left on device` during `make modules` | kernel tree + output is ~6 GB | `df -h` before you start |
| `python: command not found` in a script | Ubuntu only has `python3` | use `python3`, or `make`, which probes both |

---

## Official links

Every download this project needs, from the vendor. Nothing else is required:
there are no Python dependencies, no pip, no npm.

| What | Where | Why |
|---|---|---|
| Zorin OS documentation | <https://help.zorin.com/> | the distro this page leads with |
| Ubuntu package search | <https://packages.ubuntu.com/> | "does this release have that package?" |
| `gcc-10-aarch64-linux-gnu` (noble) | <https://packages.ubuntu.com/noble/gcc-10-aarch64-linux-gnu> | the one package that decides whether the kernel builds |
| `gcc-10-aarch64-linux-gnu` (jammy) | <https://packages.ubuntu.com/jammy/gcc-10-aarch64-linux-gnu> | same, for Zorin 17 / Ubuntu 22.04 |
| Linux 4.9.337 kernel source | <https://github.com/amazon-oss/android_kernel_amazon_mtk8163> | GPL-2.0, cloned at build time, never vendored |
| Alpine Linux downloads | <https://alpinelinux.org/downloads/> | base of the root filesystem |
| Alpine v3.20 armv7 minirootfs | <https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/armhf/> | `mkrootfs.sh` downloads the current one from here |
| Alpine v3.20 aarch64 packages | <https://dl-cdn.alpinelinux.org/alpine/v3.20/main/aarch64/> | `busybox-static`, for the initramfs |
| squashfs-tools | <https://github.com/plougher/squashfs-tools> | `mksquashfs` |
| QEMU user-mode docs | <https://www.qemu.org/docs/master/user/main.html> | why armv7 binaries run on x86 |
| Device tree compiler | <https://git.kernel.org/pub/scm/utils/dtc/dtc.git> | `dtc` |
| Android platform-tools | <https://developer.android.com/tools/releases/platform-tools> | `adb`, `fastboot` as a standalone zip (Windows/macOS route) |
| Docker Engine on Ubuntu | <https://docs.docker.com/engine/install/ubuntu/> | the pinned GCC 10 container |
| Docker Desktop | <https://docs.docker.com/desktop/> | the container on macOS and Windows |
| `usbipd-win` | <https://github.com/dorssel/usbipd-win> | USB passthrough, if flashing from WSL2 |
| Python | <https://www.python.org/downloads/> | 3.8+; no third-party packages used |
| Git | <https://git-scm.com/downloads> | clone, and Git Bash on Windows |

The kernel branch this pins (`cronos/lineage-18.1`) is Amazon's downstream
4.9.337 tree. If it ever moves, `KERNEL_REPO` / `KERNEL_BRANCH` override it — see
[docs/BUILD.md](BUILD.md) step 2.
