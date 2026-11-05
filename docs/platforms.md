# Which host can build which part

The honest split, because "works everywhere" usually means "we tried it on
Ubuntu once":

| Step | Linux | macOS | Windows | Raspberry Pi |
|---|---|---|---|---|
| Host tests (`vela.py test`) | yes | yes | yes | yes |
| Boot image, DTB, cpio, kconfig tools | yes | yes | yes | yes |
| Release packaging | yes | yes | yes | yes |
| `slotctl` A/B tests (needs a C compiler) | yes | yes (Xcode CLT) | no native | yes |
| Flash over USB (`adb`/`fastboot`) | yes | yes | yes | yes (if it has USB host) |
| Kernel build (arm64 cross) | yes | **container only** | **container only** | yes on 64-bit, else container |
| Rootfs build (chroot + QEMU + `mksquashfs`) | yes | **container only** | **container only** | yes |

Nothing in the top half needs root, Docker, or a device. That is deliberate: it
is the part reviewers and CI touch.

## Linux

Debian/Ubuntu:

```sh
sudo apt install build-essential git python3 bc bison flex libssl-dev \
     gcc-aarch64-linux-gnu qemu-user-static binfmt-support \
     squashfs-tools cpio device-tree-compiler android-tools-adb \
     android-tools-fastboot
```

Fedora: `dnf install make git python3 bc bison flex openssl-devel
gcc-aarch64-linux-gnu qemu-user-static squashfs-tools cpio dtc
android-tools`.

Arch: `pacman -S base-devel git python bc bison flex aarch64-linux-gnu-gcc
qemu-user-static squashfs-tools cpio dtc android-tools`.

Then, on any of them:

```sh
python3 tools/vela.py doctor     # confirms every box above
python3 tools/vela.py test
```

## Raspberry Pi

A 64-bit Pi 4/5 is a competent build host, and this project is small enough that
it is not a joke.

- **64-bit Pi OS (arm64):** everything works. `gcc-aarch64-linux-gnu` is a native
  compiler there, so the kernel build is *not* a cross-build and takes 1-3 hours
  instead of 20 minutes.
- **32-bit Pi OS:** build in the container. There is no armhf-hosted aarch64
  cross toolchain worth fighting for.

Two Pi-specific footguns:

1. **RAM.** `make -j$(nproc)` on a 2 GB Pi during a kernel link will OOM and
   produce `cc1: out of memory` half an hour in. Use `JOBS=2`, and add swap.
2. **SD card.** The kernel tree plus build output is ~6 GB. A card that reports
   `No space left on device` during `make modules` corrupts nothing, but it does
   waste the run, so check `df -h` first.

## macOS

```sh
xcode-select --install                 # clang, plus make
brew install python3 android-platform-tools
python3 tools/vela.py doctor
python3 tools/vela.py test           # python suite + slotctl (needs clang)
```

`slotctl` is POSIX C, so it builds and its 36 checks run natively. What does not
run natively is the kernel and rootfs: they need `chroot`, `binfmt_misc` and a
Linux cross toolchain. Use the container:

```sh
python3 tools/vela.py docker rootfs
python3 tools/vela.py docker kernel-check   # full build: docker kernel
```

**Case sensitivity.** The Linux kernel source tree assumes a case-sensitive
filesystem; on case-insensitive APFS the build can fail with a missing-header
error that looks like a broken source tree. If you insist on building natively
(don't), make a case-sensitive volume:

```sh
hdiutil create -type SPARSE -fs 'Case-sensitive APFS' -size 40g ~/vela.dmg
hdiutil attach ~/vela.dmg
```

Serial console on macOS works: `screen /dev/tty.usbserial-XXXX 921600`, exit
with `Ctrl-A K`.

## Windows

Two supported routes, in order of preference.

**1. WSL2 (fastest, least surprising).** Install Ubuntu from the Microsoft
Store, then follow the Linux instructions inside it. Flashing from WSL2 to a USB
device needs `usbipd-win`; flashing from Windows itself with the route below is
easier.

**2. Docker Desktop + Git Bash.**

```powershell
# in PowerShell, from the repo root
python tools\vela.py doctor
python tools\vela.py test              # python suite; shell suites skip
python tools\vela.py docker rootfs     # Linux-only steps in the container
python tools\vela.py docker kernel-check
python tools\vela.py release           # produces dist\
```

Install Python from python.org (tick "Add python.exe to PATH") and Git for
Windows (which gives `sh`, so `vela.py test` also runs the shell suites; the C
compiler is still missing, so `slotctl` stays skipped unless you use
`vela.py docker test`).

For flashing from native Windows: unzip the
[platform-tools](https://developer.android.com/tools/releases/platform-tools)
package and run `fastboot.exe` from that folder.

**Line endings.** `.gitattributes` pins `*.sh` and `*.py` to LF. If you cloned
before that file existed, re-checkout:

```sh
git rm --cached -r . && git reset --hard
```

A CRLF shell script fails with `bad interpreter: No such file or directory`,
which reads like a missing dependency and is not.

## The container

```sh
docker build -t vela-build .
docker run --rm --privileged -v "$PWD:/work" -w /work vela-build \
    bash -lc 'tools/mkrootfs.sh'
```

`--privileged` is there because `tools/mkrootfs.sh` chroots and runs armv7
binaries under QEMU. Without it, the explicit `qemu-arm-static` invocation still
works but `apk`'s armv7 triggers may not.

On macOS and Windows, Docker Desktop runs a Linux VM where `binfmt_misc` for arm
is normally already registered by `qemu-user-static`. If `apk add` fails with
`Exec format error`, the build is telling you exactly that and nothing else.

The image is pinned to **Debian bullseye** on purpose: its
`gcc-aarch64-linux-gnu` is GCC 10, and Linux 4.9 does not build with GCC 12+.
A newer base image costs an hour of unrelated compiler errors.

## What to expect per host, in numbers

| | Ubuntu x86_64 | M-series Mac, container | Windows, container | Pi 5 (arm64) |
|---|---|---|---|---|
| `vela.py test` | ~2 s | ~3 s | ~3 s | ~4 s |
| container build | 3-5 min | 5-8 min | 5-8 min | 3-5 min |
| kernel build | 15-30 min | 25-50 min | 25-50 min | 1-3 h |
| rootfs build | ~1 min | ~2 min | ~2 min | ~1 min |

The rootfs is small enough that the QEMU emulation cost is dominated by network
time, not CPU.
