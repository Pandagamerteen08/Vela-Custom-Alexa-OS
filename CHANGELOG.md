# Changelog

All notable changes to Vela. Dates are UTC.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html), with
one deviation: while the project is pre-hardware, `0.x` minor bumps may break the
on-disk `manifest.json` and `slot.ptr` formats, and each such bump says so.

## [Unreleased]

### Added

- First boot now comes up with a network and no console step: `vela-net` is in the
  `default` runlevel, and `/usr/sbin/vela-wifi-config` turns `SSID`/`PSK` from
  `/data/wifi.conf` into a 0600 supplicant config in tmpfs, so the read-only image
  never holds credentials. Bad or missing credentials leave Wi-Fi down with one
  warning instead of a supplicant that silently never associates.
  `tools/deploy.sh wifi <ssid> <psk>` provisions it over adb.
- The rootfs builds `wpa_supplicant` **2.9 from source** inside the chroot, so the
  build no longer depends on a package no 3.20 repository carries:
  `rootfs/wpa-2.9.config` is PSK-only (`CONFIG_DRIVER_NL80211`, no wext, no
  D-Bus, no TLS library, so the result links libc and libnl-3 only), the tarball
  is SHA-256-pinned with `WPA_SRC_TARBALL=` for offline builds, and the packed
  binary is run under qemu and must report `v2.9`. `--no-wpa` skips the step.
- `tools/vela.py` — one cross-platform entry point (`doctor`, `test`,
  `release`, `docker`) so the host tooling runs on Linux, macOS and Windows
  alike, with no bash requirement.
- `Dockerfile` — a pinned Debian bullseye image whose `gcc-aarch64-linux-gnu` is
  GCC 10, the newest compiler this Linux 4.9 tree builds with. Two stages, so the
  aarch64 initramfs busybox comes from Alpine's `apk` instead of hand-extraction.
- Release packaging: `python tools/vela.py release` writes
  `dist/vela-<version>.tar.gz`, a `.zip`, `SHA256SUMS.txt`, `release.json`
  and a generated `RELEASE.md` — with no third-party Python packages.
- `.github/workflows/ci.yml` — host checks on Ubuntu, macOS and Windows, plus a
  container job for the Linux-only steps.
- `.github/workflows/release.yml` — pushing a `v*` tag builds the pack and
  attaches it to the GitHub release, using the per-run `GITHUB_TOKEN`.
- `docs/platforms.md`, `docs/RELEASE.md`, `docs/net.md`.
- `docs/host-setup.md` — the install block for Zorin OS / Ubuntu / Debian with an
  official link for every package and download, and `vela.py doctor` now turns
  the missing tools into one copy-pasteable install command for the package
  manager it finds (`apt`, `dnf`, `pacman`, `brew`).
- `.gitattributes` pinning shell scripts to LF: a Windows checkout with CRLF
  line endings produces `bad interpreter: No such file or directory`, which is
  the least useful error message in this project.
- `LICENSE` (MIT), with a scope note about the GPL-2.0 kernel tree.

### Changed

- Shell tools probe for `python` vs `python3`, `nproc` vs `getconf` vs `sysctl`,
  and `stat -c` (GNU) vs `stat -f` (BSD/macOS) instead of assuming GNU userland.
- `tools/mkrootfs.sh` warns, rather than refuses, when arm binfmt is missing: the
  explicit `qemu-arm-static` chroot path does not need it, and containers without
  `--privileged` are a supported way to build.
- The Alpine minirootfs version is sorted with Python instead of `sort -V`, which
  BSD `sort` does not have.

### Fixed

- `rootfs/packages.txt` pinned `wpa_supplicant=2.9-r6`, a version no Alpine
  release has ever shipped, and `docs/BUILD.md` offered `v3.17` as the fallback
  branch — which carries 2.10, the version that deauths on this driver. The pin
  is now gone entirely: 2.9 is built from source in the chroot, and `docs/net.md`
  lists which branch carries which version, read from each branch's `armhf`
  `APKINDEX`.
- `kernel/build.sh --check` fetched the 2 GB kernel tree before deciding it was
  only checking: in CI that turned a sanity gate into a clone that blew the job.
- The initramfs busybox stage in the `Dockerfile` installed into an empty root
  with no keyring, so `apk` refused the packages as untrusted. `--initdb
  --keys-dir /etc/apk/keys` fixes it.
- Both inittabs respawned `/usr/sbin/velad` directly. Until the daemon exists
  that is a fork/exec loop with nothing on the panel and nothing on the console,
  which reads exactly like a broken kernel. `::respawn:/sbin/vela-run` execs
  `velad` when present and drops to a console shell when it is not.
- `tests/test_tools.py` skipped the whole cpio suite on hosts that cannot create
  symlinks (Windows without the privilege) instead of failing as if the encoder
  were wrong.
- `tests/mkrootfs.sh` asserted that the test tree's own `inittab` survived,
  which is wrong: the overlay is authoritative and overwrites it. The check now
  asserts the OpenRC-free variant really has no OpenRC and does start `velad`.
- `vela.py doctor` and `make toolchain` probed only for `aarch64-linux-gnu-gcc`,
  but `gcc-10-aarch64-linux-gnu` — the package every doc tells you to install —
  ships just `aarch64-linux-gnu-gcc-10`. A host set up exactly as documented was
  reported as missing its kernel toolchain and told to install what it already
  had, which is how a correct Zorin box gets called a broken one. Both now accept
  the versioned driver, and a unit test pins it.
- `docs/platforms.md` told Fedora and Arch readers to install the distribution
  cross GCC with no version gate. Arch ships 16.2.0 (Arch package API) and
  Fedora's tracks its own release, so both are far too new for a 4.9 tree. The
  section now says to check `aarch64-linux-gnu-gcc -dumpversion` and use the
  container when it reports 12 or more.
- `docs/design.md` still prescribed plain `gcc-aarch64-linux-gnu` in its P0
  install block (GCC 11 on jammy, 13 on noble) — the compiler that cannot build
  this 4.9 tree, and the one the other three docs were already fixed to avoid.
  `kernel/build.sh`'s not-found message now names the GCC 10 package and the
  `aarch64-linux-gnu-10-` prefix.

## [0.1.0] - 2026-11-05

First public cut: design, tooling and build system. **Nothing has booted on
hardware yet**, and the kernel build has never been executed by the author.

### Added

- `docs/design.md` — architecture, RAM/flash budgets, driver ranking,
  risks, acceptance tests, sources.
- `docs/BUILD.md` — step-by-step build from host packages to first boot.
- `docs/install.md` — flashing, partition prep, dark-panel triage, rollback.
- Kernel config layering: `cronos.fragment` (safe base), `cronos-trim.fragment`
  (opt-in, risky), `cronos-dev.fragment` (debug), with post-`olddefconfig`
  assertions in `cronos.assert`.
- `tools/mkbootimg.py`, `tools/fdt.py`, `tools/patch-dtb.py`, `tools/mkcpio.py`,
  `tools/kconfig.py`, `tools/packages.py`, `tools/report-sizes.py` — all
  dependency-free.
- `tools/mkinitramfs.sh`, `tools/mkrootfs.sh`, `tools/deploy.sh`,
  `tools/measure.sh`.
- `initramfs/init` — slot selection, mounts, `switch_root`, rescue shell.
- `src/slotctl/slotctl.c` — 16-byte dual records, CRC32, trial boots, revert.
- `rootfs/packages.txt` with a machine-enforced justification per package.
- `rootfs/overlay/` — two inittabs, two init.d services, `asound.conf`,
  `wpa_supplicant.conf`.
- Host test suite: 42 Python unit tests, 36 slot machine checks, rootfs packer
  checks including the 20 MB gate.

<!-- Relative links, so a repository rename does not break them. -->
[Unreleased]: ../../compare/v0.1.0...HEAD
[0.1.0]: ../../releases/tag/v0.1.0
