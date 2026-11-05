# Installing cronos-min

Order matters, and two of these steps are irreversible on most units. Read the
whole page before running anything.

## 0. What you need

- An Echo Show 5 2nd gen with **amonet v2.x** already applied (kaeru LK in
  `expdb`, `product: CRONOS`, TWRP in the `recovery` partition). cronos-min
  assumes that is done; it does not unlock anything.
- `fastboot` and `adb` on the host.
- A static **aarch64** busybox for the initramfs (see `tools/mkinitramfs.sh`).
- A UART adapter on the mainboard pads, if you can manage it. `ttyMT0` at
  921600 baud turns every failure below from a mystery into a log line. Without
  it, a dark panel tells you nothing.

## 1. Back up before you write

From TWRP (or adb root on LineageOS), copy the partitions you are about to
overwrite. These are the only ones this project touches:

```sh
adb shell 'dd if=/dev/block/mmcblk0p9  of=/sdcard/boot-stock.img'
adb shell 'dd if=/dev/block/mmcblk0p12 of=/sdcard/system-stock.img'   # 3 GB
adb shell 'mkdir -p /sdcard/cronos && mv /sdcard/boot-stock.img /sdcard/cronos/'
```

Keep `boot-stock.img` on the host *and* on `/sdcard`. TWRP can always `dd`
either one back without a PC.

**Never write p1–p8, p10, p11, p13–p15.** Most cronos units cannot enter BROM
USBDL, so a bad write to preloader, LK or TEE is a dead device, and anti-rollback
fuses are one-way. `tools/deploy.sh` refuses those partitions; do not work around
it with a manual `dd`.

## 2. Prepare the store partition

`system` (p12) becomes `/store` and `userdata` (p16) becomes `/data`.

From TWRP:

```sh
# reformat the system partition as a normal ext4 filesystem
mke2fs -t ext4 -L cronos-store -m 0 /dev/block/mmcblk0p12
mkdir -p /mnt/store && mount -t ext4 /dev/block/mmcblk0p12 /mnt/store
mkdir -p /mnt/store/slots/a /mnt/store/slots/b /mnt/store/boot

# the known-good boot image goes here before you ever flash ours
adb push boot-stock.img /mnt/store/boot/good.img
```

`/data` keeps its filesystem if you do not reformat it; a fresh install should
`mke2fs -t ext4 /dev/block/mmcblk0p16` so no Android state is inherited.

## 3. Build

```sh
make test
make check
kernel/build.sh            # build/boot.img
make sizes                 # must pass the gates
```

If `build/boot.img` is over 15.5 MiB it will not boot and LK will not warn you
usefully. Fix the size first: the usual culprits are a non-trimmed kernel config
and an initramfs with a module copy it does not need.

## 4. First flash

```sh
tools/deploy.sh boot build/boot.img        # prints the exact commands, writes nothing
tools/deploy.sh boot build/boot.img --yes  # actually flashes p9
```

The first boot has no rootfs yet, so the initramfs will drop to its rescue shell
(this is expected). It proves the kernel starts, the DTB is sane, and the eMMC
partitions are visible. That is the milestone worth stopping at.

Then push a slot image from the rescue shell or TWRP and arm it:

```sh
slotctl -f /store/slot.ptr init       # slot A, committed
tools/mkrootfs.sh                     # (P3) -> build/root.squashfs
tools/deploy.sh store build/root.squashfs a
```

## 5. If the panel stays dark

Work through this in order; each step is designed to produce information rather
than another guess.

1. **Does fastboot still answer?** `fastboot devices`. If yes, LK is alive and
   the failure is in `boot.img` only. If no, you are in TWRP/brick territory and
   the rest of this list does not apply.
2. **Flash the dev image** (`kernel/build.sh --dev`, then
   `tools/deploy.sh boot build/boot.img.dev --yes`). It enables `CONFIG_VT`,
   fbcon and a serial console. Check the serial pads before anything else.
3. **Serial silence usually means the kernel never decompressed.** Check the
   ramdisk compression against `CONFIG_RD_*`, and remember the header must be
   v0/page 2048 — `tools/mkbootimg.py info` prints what you built.
4. **Kernel log appears, then nothing:** the DTB or the initramfs is at fault.
   `cronos-init:` lines mean `/init` ran; their absence means the ramdisk did not
   unpack or `/init` is not executable (check the cpio modes with
   `tools/mkcpio.py list`).
5. **`cronos-init:` lines then a prompt:** that is the rescue shell working.
   `slotctl get`, `ls /store/slots/*`, `dmesg | tail`.
6. **Restore**: `fastboot flash boot <boot-stock.img>`, or in TWRP
   `dd if=/sdcard/cronos/boot-stock.img of=/dev/block/mmcblk0p9`.

Do not flash our kernel to `recovery`. LK's `recovery` slot boots **32-bit
kernels only**; a 64-bit image written there produces no output and a watchdog
reset. TWRP in `recovery` is your local recovery path — keep it.

## 6. Verify against the acceptance tests

`docs/cronos-min-design.md` §10 has twelve tests. The ones that need no
instrumentation and should be run on day one:

```sh
tools/measure.sh            # RAM used, per-process RSS, slab, thermals
tools/measure.sh flash      # what the slots actually cost
tools/measure.sh watch      # 24 h trace; a rising slope is a failed build
```

Idle RAM under 100 MiB is the gate. If it is 140 MiB, run `measure.sh` again with
the UI stopped, then with `wpa_supplicant` stopped, to find which of the three
resident processes is the difference — that is the whole diagnosis, because there
is nothing else running.
