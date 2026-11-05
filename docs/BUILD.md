# Building cronos-min, start to finish

This is the long version. It assumes you have never built a kernel for this
device and takes you from a stock host to a unit that boots a clock. Every step
states what success looks like and what the common failure is, because on this
board a wrong step usually produces *nothing* rather than an error.

Read `docs/cronos-min-design.md` for the why, and `docs/install.md` for the
flashing safety rules in isolation. Time estimates assume one person who has
built a kernel before.

**Where this stands:** steps 0–2 are host-verified in this repository. Steps 3–8
have never been run on hardware by anyone. Treat every device step as an
experiment with a known rollback.

---

## Step 0 — Host prerequisites

Ubuntu/Debian names; substitute for your distro.

```sh
sudo apt install build-essential git python3 bc bison flex libssl-dev \
     gcc-aarch64-linux-gnu binutils-aarch64-linux-gnu \
     qemu-user-static binfmt-support squashfs-tools cpio \
     android-tools-adb android-tools-fastboot device-tree-compiler
```

Why each matters:

| Tool | Used for | Missing → |
|---|---|---|
| `gcc-aarch64-linux-gnu` | the kernel (arm64) | `build.sh` stops with a clear message |
| `qemu-user-static` + `binfmt` | running armv7 `apk` triggers on x86 | rootfs package triggers silently skipped |
| `mksquashfs` | packing the rootfs | `mkrootfs.sh` refuses |
| `dtc` | inspecting DTBs (optional; `tools/fdt.py` is self-contained) | tests only check round-trip |
| `adb`/`fastboot` | flashing, pushing slots | `deploy.sh` refuses |

**Toolchain warning, and it is the first thing that will go wrong:** this is
Linux 4.9. GCC 12 and newer reject constructs that 4.9 relies on, and the MTK
vendor tree is worse than mainline. Use GCC 6–10, or AOSP clang 10–12.
`kernel/build.sh` prints a warning for GCC ≥ 12 but does not stop — you will get
a wall of unrelated compile errors if you ignore it.

If you need older GCC:

```sh
sudo apt install gcc-10-aarch64-linux-gnu
CROSS_COMPILE=aarch64-linux-gnu-10- kernel/build.sh
```

**Optional but decisive:** a UART adapter on the mainboard pads (`ttyMT0`,
921600 8N1). Without it, "the panel is black" is the entire diagnostic. With it,
every failure on this page is a log line.

---

## Step 1 — Get the repository and prove the tooling

```sh
git clone <this repo> cronos-min
cd cronos-min
make test
```

Expected:

```
== python unit tests
Ran 42 tests ... OK
   tools: PASS
== slotctl state machine
   slotctl: PASS
== rootfs packer + package contract
   rootfs: PASS
ALL CHECKS PASSED
```

If `mksquashfs` is absent the rootfs section says `SKIP` and exits 0 — install it
before step 4 rather than discovering it there.

Nothing in this step needs a device, a kernel tree, or root.

---

## Step 2 — Build the kernel and the boot image

```sh
kernel/build.sh --check      # toolchain + source sanity, no compile
kernel/build.sh              # full build
```

What happens, in order, and what to expect:

1. **Clone.** `amazon-oss/android_kernel_amazon_mt8163`, branch
   `cronos/lineage-18.1`, shallow. ~2 GB. If the branch name has changed, set
   `KERNEL_REPO` / `KERNEL_BRANCH`; the script prints the available defconfigs
   if the one it wants is missing.
2. **defconfig**, then **fragments** applied in place, then `olddefconfig`, then
   **assertions**. This order matters: `olddefconfig` may flip a symbol off when
   a dependency is missing, which is why the assertion pass runs *after* it.
   ```
   [kconfig] CONFIG_SQUASHFS: <unset> -> y   (cronos.fragment:27)
   ...
   [kconfig] 12 symbol(s) changed
   [kconfig] all assertions satisfied
   ```
   An assertion failure means something you asked for was overridden. Do not
   proceed; find the dependency.
3. **Compile** `Image.gz`, `dtbs`, `modules`. 10–40 minutes.
4. **DTB patch**: `amzn,mic-downmix` is deleted so the two microphones stop being
   averaged into one.
   ```
   [patch-dtb] removed amzn,mic-downmix from /soc/.../sound
   ```
   A warning that it was not found is not fatal, but if this is the real DTB it
   means the property name differs — check with `tools/patch-dtb.py <dtb> --list`.
5. **initramfs**: built if absent. Requires a **static aarch64 busybox** (the
   kernel is 64-bit; the rootfs is armv7). Get one from Alpine:
   ```sh
   apk fetch --arch aarch64 --root /tmp/bb busybox-static
   ```
   The script refuses an x86 or armv7 binary by name and architecture.
6. **Pack** `build/boot.img` with `--cmdline-file kernel/cmdline.txt`.

Check the result:

```sh
python3 tools/mkbootimg.py info build/boot.img
make sizes
```

`info` must show `header_version 0`, `page_size 2048`, `kernel 0x40080000`,
`ramdisk 0x69244e00`, `tags_addr 0x48000000`. Anything else will not boot under
amonet's LK.

`make sizes` must pass. Typical: kernel ~6 MB + initramfs ~1.5 MB ≈ 7.5 MB
against a 15.5 MB gate.

**Before you go further, copy the cmdline from the stock image** and merge
anything device-specific into `kernel/cmdline.txt`:

```sh
python3 tools/mkbootimg.py unpack stock-boot.img --outdir /tmp/stock
cat /tmp/stock/cmdline.txt
```

The stock cmdline carries MTK boot options that are cheap to keep. The one flag
that must be present in ours is `firmware_class.path=/lib/firmware` (the Android
default points at `/vendor/firmware`, which does not exist on our rootfs).

---

## Step 3 — Build and test the initramfs on its own (optional but cheap)

```sh
tools/mkinitramfs.sh --busybox /path/to/aarch64-busybox --out build/initramfs.cpio.gz
tools/mkcpio.py list <(gzip -dc build/initramfs.cpio.gz) 2>/dev/null || \
  sh -c 'gzip -dc build/initramfs.cpio.gz > /tmp/i.cpio && tools/mkcpio.py list /tmp/i.cpio'
```

You should see `./init`, `./bin/busybox`, the applet symlinks, and `./bin/slotctl`.
If `slotctl` is missing the device cannot choose a slot — the build must fail, and
it does.

---

## Step 4 — Build the rootfs

Two modes.

**Plan only (safe, no root):**

```sh
tools/mkrootfs.sh --dry-run
```

**Full build (needs root for chroot):**

```sh
sudo tools/mkrootfs.sh
```

It downloads the armv7 minirootfs, copies `qemu-arm-static` in, runs
`apk add` *inside* the chroot (the armv7 package triggers are the whole point),
strips inside the chroot, applies `rootfs/overlay/`, creates the mountpoints,
verifies `/sbin/init`, `/bin/busybox`, `/etc/inittab` exist, then packs:

```sh
mksquashfs tree root.squashfs -comp xz -b 131072 -no-xattrs -all-root -noappend
```

Output: `build/root.squashfs` and `build/manifest.json` (sha256, size, build id).

**Expect a real fight here with one package.** `wpa_supplicant` is pinned to
`2.9-r6` because 2.10+ advertises RSN replay counters the vendor driver does not
implement, and every AP then deauths with reason 2 — which looks exactly like a
wrong passphrase and can waste a day. Alpine 3.20 does not ship 2.9. Your
options, in order of preference:

1. Build 2.9 from source in the chroot (see `docs/net.md` when it exists).
2. Point `MIRROR`/`ALPINE_BRANCH` at `v3.17` for that one package.
3. Ship 2.11 and accept that Wi-Fi will deauth; do not do this and then debug
   the passphrase.

The 20 MB gate is enforced. If it fails, find the growth before arguing with the
gate:

```sh
du -a build/rootfs-tree | sort -h | tail -20
```

**Variant B**, if you end up fighting the RAM gate:

```sh
sudo tools/mkrootfs.sh --variant inittab
```

This drops OpenRC, uses the busybox-only inittab, and saves ~2 MB RSS at boot.

---

## Step 5 — Prepare the device (TWRP + partition layout)

Prerequisites: amonet v2.x already applied, TWRP in `recovery`, device charged.

```sh
adb reboot recovery        # or the button combo
adb devices                # must list the device in recovery
```

Back up what you will overwrite — from TWRP's shell:

```sh
adb shell 'dd if=/dev/block/mmcblk0p9  of=/sdcard/boot-stock.img'
adb shell 'dd if=/dev/block/mmcblk0p12 of=/sdcard/system-stock.img'
adb pull /sdcard/boot-stock.img .
```

Create the store filesystem and the slot skeleton:

```sh
adb shell 'mke2fs -t ext4 -L cronos-store -m 0 /dev/block/mmcblk0p12'
adb shell 'mkdir -p /mnt/store && mount -t ext4 /dev/block/mmcblk0p12 /mnt/store'
adb shell 'mkdir -p /mnt/store/slots/a /mnt/store/slots/b /mnt/store/boot'
adb push boot-stock.img /mnt/store/boot/good.img
adb shell 'mke2fs -t ext4 -L cronos-data /dev/block/mmcblk0p16'
```

**Do not touch p1–p8, p10, p11, p13–p15.** Most cronos units cannot enter BROM
USBDL; a bad preloader/LK/TEE write is unrecoverable. `recovery` keeps TWRP and
boots 32-bit kernels only, so never flash our 64-bit kernel there.

---

## Step 6 — First flash: kernel only

```sh
tools/deploy.sh boot build/boot.img          # dry run: prints, writes nothing
tools/deploy.sh boot build/boot.img --yes    # writes p9 and reboots
```

### What "success" looks like on this first boot

There is no rootfs yet, so the initramfs is the destination. Expect, in order:

1. The Amazon logo (p5) or a black panel — the kernel has not painted anything.
2. `cronos-init: store mounted from /dev/block/mmcblk0p12`.
3. `cronos-init: no slot record; assuming a fresh install` (or the rescue shell).
4. A prompt if a serial console is attached.

If you have serial, the first 200 lines of dmesg are the whole diagnosis. If you
do not, and the panel stays black past ~10 s, go to step 9.

---

## Step 7 — Install a slot and boot the rootfs

```sh
adb push build/root.squashfs /tmp/root.squashfs.new
adb shell 'mount -t ext4 /dev/block/mmcblk0p12 /mnt/store 2>/dev/null || true'
adb shell 'mkdir -p /mnt/store/slots/a'
adb shell 'cp /tmp/root.squashfs.new /mnt/store/slots/a/root.squashfs.new'
adb shell 'sync && mv /mnt/store/slots/a/root.squashfs.new /mnt/store/slots/a/root.squashfs && sync'
adb shell 'slotctl -f /mnt/store/slot.ptr init'     # slot A, committed
adb shell reboot
```

The write-then-rename matters: a half-written `root.squashfs` in the slot you are
booting is a rescue-shell boot, and `rename` is atomic.

Expected on boot: initramfs lines → `rootfs mounted (read-only squashfs)` →
`switch_root`. Then busybox init starts `cronos-boot`, and — because `cronosd`
does not exist yet — you get a shell on the console instead of a respawn loop.
That is the correct behaviour for a P3 build and is worth confirming explicitly.

For a first install you can also arm the slot with trials to test rollback:

```sh
adb shell 'slotctl -f /mnt/store/slot.ptr pending'   # B, 3 trials
```

---

## Step 8 — Measure, and only then believe the numbers

```sh
tools/measure.sh             # RAM used, per-process RSS, slab, thermals
tools/measure.sh flash       # what the slots actually cost
tools/measure.sh watch       # one sample a minute into /data/log/ram.log
```

The gate is `MemTotal − MemAvailable` under 100 MiB at idle, with **no upward
slope** over 24 hours. A flat 90 MiB is fine; a 60 MiB that grows 1 MiB/hour is a
failed build, because in 30 days it is not 60 MiB.

If it is over: stop the UI, measure again; stop `wpa_supplicant`, measure again.
With three resident processes that difference identifies the culprit completely —
there is nothing else running.

---

## Step 9 — When it does not boot

Work down this list; each step produces information rather than another guess.

1. **`fastboot devices` answers?** LK is alive, the fault is in `boot.img`.
2. **Flash the dev kernel** (`kernel/build.sh --dev`) and attach serial. It turns
   on `CONFIG_VT`/fbcon, `DYNAMIC_DEBUG`, kallsyms and a serial console. Costs
   3–4 MB, buys all the information.
3. **No serial output at all** → the kernel did not decompress. Check the
   ramdisk compression against `CONFIG_RD_GZIP`, and re-check the header with
   `tools/mkbootimg.py info`.
4. **Serial output stops early** → DTB. Boot the stock DTB once to confirm the
   difference, and check that the DTB you appended is the one you patched.
5. **`cronos-init:` lines then a prompt** → the initramfs is healthy and the
   problem is the slot image: `slotctl get`, `ls -l /store/slots/*/`.
6. **Everything works but `/data` is missing** → by design the UI still runs;
   check `dmesg | grep mmcblk0p16` and whether `mke2fs` in step 5 succeeded.
7. **Rollback**: `fastboot flash boot /store/boot/good.img`, or in TWRP
   `dd if=/sdcard/boot-stock.img of=/dev/block/mmcblk0p9`.

A watchdog reset loop (device reboots every ~30 s) means LK never got control
back — check the `boot` partition size. A 16 MB partition with an 18 MB image is
a loop, not an error message.

---

## Step 10 — What is still missing

This repository builds a kernel, an initramfs and a rootfs. It does **not** yet
contain `cronosd`, so there is no UI, no audio, no touch input and no network
client. That is the next phase and it is the largest one:

| Milestone | First demoable result |
|---|---|
| fbdev bring-up | a solid colour on the panel via `/dev/graphics/fb0` |
| rotation + LVGL | a landscape 960×480 UI rotated into the portrait framebuffer |
| touch | a tap moves a rectangle (GT9xx is the least risky subsystem) |
| audio out | a 1 kHz tone with the DL1 hold in place |
| Wi-Fi | `wpa_supplicant` association + one TCP connect |
| wake word | INT8 model fires on "hey jarvis" at 1 m |

Build them in that order. Each one is independently demoable, and the order is
chosen so that a failure is localised.
