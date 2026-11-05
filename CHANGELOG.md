# Changelog

All notable changes to Vela. Dates are UTC.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html), with
one deviation: while the project is pre-hardware, `0.x` minor bumps may break the
on-disk `manifest.json` and `slot.ptr` formats, and each such bump says so.

## [Unreleased]

### Added

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

- `tests/mkrootfs.sh` asserted that the test tree's own `inittab` survived,
  which is wrong: the overlay is authoritative and overwrites it. The check now
  asserts the OpenRC-free variant really has no OpenRC and does start `velad`.

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
