# Vela

**A ~100 MiB Alpine Linux for the Amazon Echo Show 5 (2nd gen, 2021, `cronos`, MediaTek MT8163).**

Fire OS 7 on this device idles at roughly 700 MB of the 996 MB available. Vela
targets **under 100 MiB idle**, a **7–10 MiB** read-only squashfs root, and **three
resident processes**. Not a demo: a kernel, a userspace, an init, a service model,
A/B updates with rollback, and a build system that fails when any of those numbers
drift.

> **Status: pre-hardware.** Every host-side check passes. Nothing here has booted on
> a real unit yet. The kernel build script is reviewed but unproven. Read
> [Verified vs. assumed](#verified-vs-assumed) before you flash anything.

> **Naming:** *Vela* is the project. `cronos` is Amazon's codename for this board, so
> identifiers that describe the **hardware** keep it: `cronos.fragment`,
> `cronos_defconfig`, `mt8163-cronos.dtb`, the `cronos/lineage-18.1` kernel branch,
> the `cronos-store` filesystem label. Everything this project wrote is `vela`/`velad`.

---

## Why this is not "just build a small Linux"

The device has no usable mainline platform. MT8163 upstream support is a single RFC
series (Oct 2026) that boots one board to a UART shell — no clocks, pinctrl, power
domains, display, audio or PM — and the stock firmware has no usable PSCI, so mainline
cannot even start the secondary cores on an unlocked unit. There is no MT7668 Wi-Fi in
`mt76`, and no MT8163 ASoC or DRM driver.

So the kernel is **Amazon's downstream 4.9.337** (`cronos/lineage-18.1`), and the
optimisation happens in userspace: where the 10× actually is.

| | Stock Fire OS 7 | Vela |
|---|---|---|
| Idle RAM | ~700 MB | **55–85 MiB** (gate: 100 MiB) |
| Root filesystem | ~1.2 GB | **7–10 MiB** squashfs (gate: 20 MiB) |
| `boot.img` | ~10 MB | **9.5–13 MiB** (gate: 15.5 MiB; partition is 16 MB) |
| Resident processes | hundreds | **3** (init, `velad`, `wpa_supplicant`) |
| Update model | Amazon OTA | signed A/B slots, 3 trial boots, automatic rollback |

---

## Architecture

```
p9 boot (16 MB)
  └─ Android boot image v0, page 2048, arm64 4.9.337 kernel + appended patched DTB
     + aarch64 initramfs (busybox + slotctl + /init)
        ↓
  mount p12 ext4 (/store) → slotctl picks slot A or B
  loop-mount /store/slots/<x>/root.squashfs   (read-only, xz)
  tmpfs on /var /run /tmp   ·   mount p16 ext4 (/data)
        ↓ switch_root
  PID 1 busybox init → OpenRC (2 services) → velad, wpa_supplicant
        ↓
  /dev/graphics/fb0 (fbdev, no DRM, no compositor) + LVGL 9, software 90° rotation
```

Four constraints drive the whole design, and each was verified on the device before
being designed around:

- **The framebuffer is portrait 480×960 BGRA with `line_length` 1920 and a 480×1920
  virtual page.** Compose landscape 960×480, rotate in software, and flip pages with
  `FBIOPAN_DISPLAY` — free double buffering, because the second page already exists.
- **fbdev only works because nothing sends an Android frame config.** `CONFIG_FREE_FB_BUFFER=y`
  frees the 7 MB boot framebuffer when Android's first frame arrives; a pure Linux boot
  never triggers it. The vendor `disp_mgr` overlay path accepts configs and never
  latches, so it is not an alternative.
- **Audio has three non-negotiable invariants.** Hold an unused AFE PCM
  (`/dev/snd/pcmC0D1c`) open or the DL1 SRAM path panics the kernel; never write
  `Ext_Speaker_Amp_Switch` (it toggles the amp reset, `gpio-392`, and the driver never
  re-inits); clear `Speaker Safe Mode A` (30 dB of output). Capture is S24_3LE 4ch
  16 kHz, playback S16_LE 2ch 48 kHz.
- **The `recovery` partition boots 32-bit kernels only.** Our kernel is arm64, so it
  goes in `boot`, and TWRP stays in `recovery` as the only local undo.

Full reasoning, tables and sources: **[docs/design.md](docs/design.md)**.
Step-by-step build: **[docs/BUILD.md](docs/BUILD.md)**. Flashing and recovery:
**[docs/install.md](docs/install.md)**.

---

## Verified vs. assumed

| Check | Result |
|---|---|
| Boot image header byte offsets vs. AOSP v0 | **passes** (`tests/test_mkbootimg.py`) |
| Boot image round-trip + 16 MB partition gate | **passes** |
| FDT parse/patch round-trip | **passes**, including a real `dtc`-produced blob |
| cpio determinism, hardlinks, symlinks | **passes**, cross-checked with GNU `cpio` |
| Kernel config fragments: apply, idempotence, assertions | **passes** |
| A/B slot state machine: trials, revert, commit, corruption, double failure | **passes**, 38 checks |
| Rootfs packer: overlay, boot-path checks, squashfs, 20 MB gate | **passes** (with a prepared tree; `apk` needs QEMU) |
| Package list contract (every package justified) | **passes**, enforced in CI-able form |
| initramfs → cpio → gzip → boot.img end to end | **passes**, with a synthetic kernel |
| Arch guard rejects an x86 busybox in the initramfs | **passes** (by design) |
| Host tooling on Linux, macOS and Windows | **runs** — `python tools/vela.py test`, CI matrix in `.github/workflows/ci.yml` |
| Release packaging and checksums | **passes** — `tools/vela.py release`, verified in CI |
| **Kernel compile** | **not run** — needs an aarch64 toolchain and a 2 GB tree |
| **Anything on hardware** | **not done** |

All `(est.)` size figures in the design document stay estimates until
`tools/measure.sh` runs on a unit.

---

## Install a release

Every tagged build gets a pack on the [Releases](../../releases) page:
`vela-<version>.tar.gz`, the same tree as `.zip`, `SHA256SUMS.txt`, and a
generated `RELEASE.md` describing exactly which artifacts that build contains.

```sh
tar xzf vela-0.1.0.tar.gz && cd vela-0.1.0
sha256sum -c SHA256SUMS.txt        # Linux
shasum -a 256 -c SHA256SUMS.txt    # macOS
less RELEASE.md                    # images present, install steps, known limits
```

Releases are marked pre-release on purpose: no build here has booted on a unit
yet, and the pack says so rather than implying otherwise.

## Quickstart

```sh
python tools/vela.py doctor    # what this machine can build (any OS)
python tools/vela.py test      # every host check this host can run (any OS)
python tools/vela.py release    # package dist/ for a release (any OS)

make test        # host checks: no device, no root, no toolchain
make check       # toolchain + source sanity
make toolchain   # what is installed

kernel/build.sh            # clone + build kernel, patch DTB, pack -> build/boot.img
make rootfs-dry            # validate the package contract, print the plan
make sizes                 # fail if boot.img or the rootfs left the budget

tools/deploy.sh boot build/boot.img          # dry run: prints the commands
tools/deploy.sh boot build/boot.img --yes    # writes p9, reboots
```

Requirements that will bite you first:

- **[docs/host-setup.md](docs/host-setup.md)** — one install block for Zorin OS / Ubuntu /
  Debian, with the official link for every package and download. `python3 tools/vela.py
  doctor` prints the one line your machine is missing.

- **GCC ≤ 10** (or AOSP clang 10–12) for Linux 4.9. GCC 12+ produces a wall of
  unrelated errors.
- a **static aarch64 busybox** for the initramfs. The kernel is 64-bit; the rootfs is
  armv7. An armv7 busybox there fails with `Exec format error` before any console
  exists, which is the least diagnosable failure this board has.

### Runs on your host too

| | Linux | macOS | Windows | Raspberry Pi (64-bit) |
|---|---|---|---|---|
| host tests, boot image, DTB, cpio, kconfig, release pack | yes | yes | yes | yes |
| `slotctl` A/B tests | yes | yes (Xcode CLT) | container | yes |
| flash over USB | yes | yes | yes | yes |
| kernel + rootfs build | yes | container | container | yes |

The top two rows need no root, no Docker and no device: `python tools/vela.py
test`. The Linux-only steps are the ones that genuinely need Linux (`chroot`,
`binfmt_misc`, a cross toolchain), and `./Dockerfile` is the supported way to run
them from macOS or Windows:

```sh
python tools/vela.py docker rootfs         # or: kernel, kernel-check, test, shell
docker run --rm --privileged -v "$PWD:/work" -w /work vela-build \
    bash -lc 'tools/mkrootfs.sh && kernel/build.sh'
```

Per-OS instructions, Pi-specific footguns (2 GB RAM + `-j$(nproc)` = an OOM during
the link) and the macOS case-sensitivity trap: **[docs/platforms.md](docs/platforms.md)**.
Release process: **[docs/RELEASE.md](docs/RELEASE.md)**. Wi-Fi pitfalls:
**[docs/net.md](docs/net.md)**.

---

## Repository layout

```
docs/design.md              the design: architecture, targets, risks, acceptance tests
docs/host-setup.md          what to install, with the official link for each package
docs/BUILD.md               step-by-step build, from host packages to first boot
docs/install.md             flashing, partition prep, dark-panel triage, rollback
kernel/                     config fragments (base / trim / dev), assertions, build driver
initramfs/init              slot choice, mounts, switch_root, rescue shell (PID 1)
rootfs/packages.txt         THE package list; every line carries its justification
rootfs/overlay/             inittab (2 variants), init.d, wpa_supplicant template, asound.conf
src/slotctl/                A/B slot pointer: 16-byte dual records, CRC32, atomic rename
tools/                      mkbootimg, patch-dtb, fdt, mkcpio, kconfig, packages,
                            mkinitramfs, mkrootfs, deploy, measure, report-sizes,
                            vela.py (cross-platform entry point)
Dockerfile                  pinned Linux build env (GCC 10, QEMU, mksquashfs)
.github/workflows/           ci.yml (Linux/macOS/Windows + container), release.yml
tests/                      host tests for every tool, the state machine and the packer
```

---

## Two rules that keep it small

1. **`rootfs/packages.txt` may only gain a line with a one-sentence justification** and a
   measured flash/RSS delta. `tools/packages.py` rejects a line without one, and rejects
   a forbidden name (python3, openssl, bluez, udev, …) outright.
2. **A kernel symbol may only be removed after a boot test.** `kernel/cronos.fragment` is
   the safe set; `kernel/cronos-trim.fragment` is opt-in (`build.sh --trim`) precisely
   because a kernel that does not boot costs an evening with a serial console.

---

## Safety

`tools/deploy.sh` writes **only** `boot`, `system` and `userdata`, is dry-run by default,
and refuses anything larger than the partition. It will not touch `recovery` even if
asked, because our kernel cannot boot there and TWRP is the undo.

Most cronos units cannot enter BROM USBDL, so a bad write to preloader, LK or TEE is
unrecoverable, and anti-rollback fuses are one-way. Back up `boot` and `system` before
the first flash ([docs/BUILD.md](docs/BUILD.md) step 5).

---

## Honest limitations

- **No local speech-to-text or text-to-speech.** Vosk's small model alone is ~300 MB RSS
  on a 996 MB device. This is a voice *satellite*: it streams audio to a server.
- **No video playback.** Same reason there is no DRM path: there is no working hardware
  decode route on this kernel.
- **Kernel 4.9 is EOL** and gets no upstream fixes. The mitigation is not patching — it
  is having almost nothing running and no listening ports.
- **Bluetooth costs a kernel rebuild** (`CONFIG_BT` + `HCI_VHCI`) plus a `/dev/stpbt`
  bridge; it ships off.
- **The mute button is a hardware latch.** Software cannot release it; report its state
  in the UI honestly rather than pretending otherwise.
- **Suspend-to-RAM is unverified** on this vendor kernel and is deliberately off the
  critical path.

---

## License

MIT for everything in this repository — see [LICENSE](LICENSE). The note at the
bottom of that file matters if you plan to redistribute an image: the kernel tree
this builds (`amazon-oss/android_kernel_amazon_mtk8163`) is GPL-2.0 and is cloned
at build time, never vendored, and Amazon's bootloader, TEE and preloader are not
redistributed here in any form.

## Credits

Hardware ground truth leans heavily on the [TECHO5](https://github.com/HuskerMinion/techo5)
project (same SoC family, different board) and the amonet reverse-engineering work that
makes unlocking `cronos` possible at all. Sources are listed in
[docs/design.md](docs/design.md) §12.
