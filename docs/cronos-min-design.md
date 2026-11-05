# cronos-min

**A minimal, read-only, single-daemon Alpine Linux system for the Amazon Echo Show 5 (2nd gen, 2021) — codename `cronos`, board C76N8S.**

Working name `cronos-min`; rename freely. This document is an engineering plan, not marketing.
Every number marked **(est.)** is an estimate with a measurement gate attached. Every number
marked **(verified)** comes from a real unit, and the source is named.

Primary reference implementation: **TECHO5** (`github.com/HuskerMinion/techo5`, MIT), which
already runs Alpine on this exact board in daily use. `cronos-min` differs from it in five
deliberate ways: **squashfs slots instead of ext4 directory slots**, **a C daemon instead of a
Go daemon**, **mbedTLS instead of OpenSSL**, **no overlayfs/BlueZ/AirPlay by default**, and a
**kernel config rebuilt for a Linux-only workload instead of an Android one**. Those five
changes are where the size and RAM wins come from.

---

## 0. Executive summary

| | Stock Fire OS 7 | TECHO5 (real, measured) | **cronos-min target** |
|---|---|---|---|
| Rootfs on flash | ~2.5 GB (system+vendor+data) | ~130 MB ext4 per slot | **14–20 MB squashfs per slot (est.)** |
| Kernel image | 7.4 MB gz (Android config) | 7.4 MB gz | **5.5–7.0 MB gz (est.)** |
| Idle RAM used | ~600–700 MB of 996 MB | not published | **55–85 MB of 996 MB (gate: <100)** |
| Resident processes at idle | ~250 | ~4 | **3** (init, daemon, wpa_supplicant) |
| Root filesystem | read-write ext4 | read-only bind of a slot dir | **read-only squashfs + tmpfs** |
| Update model | Amazon OTA, no user rollback | A/B dirs, 3 trial boots, signed manifest | **A/B squashfs images, 3 trial boots, signed manifest** |
| Attack surface | Amazon cloud, Alexa, ADB, 250 services | no Amazon, SSH | **no Amazon, SSH off by default, no listening port except the HA socket out** |

The single most important architectural fact: **this device cannot run a mainline kernel.**
Its vendor firmware has no usable PSCI implementation, so mainline cannot even bring up the
secondary CPU cores without replacing TF-A, U-Boot and OP-TEE, and mainline has no MT8163
display controller, no MT8163 ASoC platform driver, no MT8163 cpufreq/cpuidle, and no MT7668
Wi-Fi. Every design decision below follows from that: **we build on the downstream Amazon
4.9.337 tree and we optimise the userspace instead of the kernel.**

---

## 1. Corrections to the brief (read this first)

These are places where the stated hardware differs from what is on the board. Getting them
wrong costs weeks.

| Brief says | Reality on cronos | Consequence for the design |
|---|---|---|
| "1 GB LPDDR3" | `MemTotal` = **996 MB** (verified, LineageOS 18.1 bench unit). One XDA claim that amonet 2.x "unlocks the full 2 GB" is **not** reflected in `MemTotal` on a 2.x unit; treat 1 GB as the hard ceiling and verify with `free -m` after unlock. | Budget against 996 MiB, not 1024. Reserve ~60 MiB of headroom for page cache and bursts. |
| "32-bit ARM / armhf" | Right about *userspace*: `armv8l`, `ABI: arm`. But the **kernel is arm64** (`Image`, 7.4 MB gzip, load `0x40080000`) with `CONFIG_COMPAT`. Only the TWRP recovery kernel is 32-bit (`zImage`, 4.9.77). | **Kernel modules must be built arm64.** Your userspace is armv7; your `.ko` files are not. Mixing these up is the most common first-build failure. |
| "panel variants: ST7701S and others" | cronos is **fixed** to `st7701s_wsvga_dsi_vdo_cronos_st_truly` (from the `lcm=` cmdline). Panel variance is real on *athena* (Show 8 gen 2) and *crown*, not here. | No panel-variant matrix needed. Do not build for hardware that isn't there. |
| "DACs: RT5616, TAS5805M, MAX98396" | cronos ships **MAX98396** (I²C 2-0x3d) **or** TAS5805M (I²C 2-0x2c). RT5616 is the **1st-gen** Show 5 (`checkers`). Mic path is a **TLV320AIC3101** ADC (I²C 0-0x18), not a DAC. | Two amp variants to handle, one mic codec. This is a small problem, not the large one the brief implies. |
| "camera: OV02B10 or similar" | Main + sub camera on I²C 0, mechanical lens cover on `gpio-499`. Exact sensor part unverified. | Treat as feature-flagged and off by default. Never on the boot path. |
| "bootloader unlocked via amonet-cronos" | Correct, but **version matters**: amonet **2.x** turns `boot` into a plain Android boot image (header v0, page 2048). amonet **1.x** uses a 1 KB microloader header at the front of `boot`/`recovery` and reports `product: CHECKERS`. | Require amonet 2.x. A 1.x unit needs the 2.x exploit run first; cronos-min will not boot on 1.x layouts. |
| "mute button acts as power in some setups" | It emits `KEY_POWER` on `gpio-privacy-button` and toggles a **hardware latch**; the red LED follows the latch. **Software cannot release the latch** — only the button can. | Mute is a UI *state display*, never a software control. Do not build a "mute" button in the UI that pretends to clear it. |
| "framebuffer/DRM paths" | fbdev works **only because a pure-Linux boot never sends the frame config that frees it** (`CONFIG_FREE_FB_BUFFER=y` frees the 7 MB boot framebuffer the first time an Android-style frame config arrives). The vendor `/dev/mtk_disp_mgr` overlay config **does not latch** from a bare session (verified: `SET_INPUT_BUFFER` + `TRIGGER_SESSION` return success, `OVL0 src_con` stays 0). | **fbdev is the only working display path on 4.9.** Not DRM. Not the vendor display-manager. Design the UI around `/dev/graphics/fb0`. |
| "OpenRC" | On Alpine, PID 1 is **busybox init** reading `/etc/inittab`; OpenRC is a service manager started *by* it. TECHO5 uses busybox init alone. | Keep OpenRC, but with a **two-service runlevel**, and keep a documented inittab-only fallback (saves ~2 MB). Details in §5. |

---

## 2. Recommended architecture

```
                    ┌─────────────────────────────────────────┐
   p9  boot 16 MB   │  LK (amonet kaeru 2.0.0)                │
                    │   ↓ Android boot image v0, page 2048    │
                    │  arm64 4.9.337 kernel  (5.5–7.0 MB gz)  │
                    │  + initramfs (busybox, slot logic,      │
                    │    rescue shell)         (3.5–5 MB gz)  │
                    └─────────────────────────────────────────┘
                                     ↓ initramfs
        ┌───────────────────────────────────────────────────────────┐
        │  mount p12 (ext4, label cronos-store)                     │
        │  read slot A/B pointer, decrement trial counter           │
        │  loop-mount /store/slots/<x>/root.squashfs  (ro)          │
        │  mount tmpfs on /var /run /tmp  (2+1+1 MB)                │
        │  mount p16 (ext4) on /data                                 │
        │  switch_root  → free initramfs pages                      │
        └───────────────────────────────────────────────────────────┘
                                     ↓
   PID 1  busybox init ── /etc/inittab
     ├─ 1  cronosd        (C, static-ish, owns fb + touch + alsa + net + ota)
     ├─ 2  wpa_supplicant 2.9  (spawned by cronosd, dies with it)
     └─    (OpenRC: 2 services, or inittab-only build)
```

### 2.1 Kernel

**Source:** `github.com/amazon-oss/android_kernel_amazon_mt8163`, branch `cronos/lineage-18.1`
(4.9.337). Non-negotiable. Alternatives evaluated and rejected:

| Option | Why rejected |
|---|---|
| Mainline 7.x (`bengris32/linux-mtk` `mt8163/7.0`) | MT8163 `mediatek-drm` exists in that tree but the **cronos DTS only enables eMMC, USB, GPIO keys and the light sensor** — no panel, no touch, no codec, no SDIO. Upstream MT8163 support is a single RFC series (Oct 2026) that boots one board to a UART shell, with no clocks, pinctrl, power domains, DRM, audio or PM. Needs custom TF-A/U-Boot/OP-TEE because **the stock firmware has no usable PSCI**. |
| LibreEcho 6.1 (`aslater3/LibreEcho-Linux-6.1`) | Real and impressive, but targets `radar_puffin`/`biscuit`; cronos is explicitly not supported, and the MT8163 changes are not upstream-ready. A 6.1 port for cronos is a multi-month project with no payoff: it buys no memory. |
| TWRP's 4.9.77 32-bit kernel | 32-bit kernel = no `CONFIG_COMPAT` needed but **the `recovery` slot is the only place it boots**, and it lacks the vendor Wi-Fi/BT modules. It is the *recovery* kernel, not the product kernel. |

**What we change in the config** (this is where cronos-min beats TECHO5 on flash):

*Enable (TECHO5's kernel does not have these — it uses ext4 directory slots because of it):*
```
CONFIG_SQUASHFS=y
CONFIG_SQUASHFS_XZ=y
CONFIG_SQUASHFS_LZ4=y
CONFIG_OVERLAY_FS=y            # dev builds only; prod uses ro + tmpfs
CONFIG_BLK_DEV_LOOP=y
CONFIG_DEVTMPFS=y              # the LineageOS config has devtmpfs NOT set
CONFIG_DEVTMPFS_MOUNT=y        # removes the whole mdev coldplug race
CONFIG_BT=y                    # required for the mt7668 HCI bridge
CONFIG_BT_HCIVHCI=y
```
*Disable, in descending order of savings (each must be boot-tested on hardware):*
```
CONFIG_DEBUG_KERNEL, DEBUG_INFO, DEBUG_FS, FTRACE, KPROBES, UPROBES,
PERF_EVENTS, MAGIC_SYSRQ, KALLSYMS_ALL, IKCONFIG, AUDIT,
F2FS, XFS, BTRFS, NFS, CIFS, UDF, ISO9660, SCSI, MD, DM_* (except DM_VERITY if wanted),
USB_STORAGE, USB_SERIAL options not used, most SND_SOC_CODECS,
DRM (entire subtree — this kernel has no usable DRM path anyway),
FB console (CONFIG_VT is already off; keep it off prod, on dev)
CONFIG_CC_OPTIMIZE_FOR_SIZE=y, CONFIG_LTO_NONE + -Os
```
*Do **not** touch until proven safe:* `ION`, `M4U`, `MTK_CMDQ`, `mtkfb`/`videox`, `mt-snd-card`
(AFE, DL1, `amzn_mt_spi_pcm`), `MTK_JSA1214`, `gpio-privacy`, `gt9xx`, `idme`, `mtk-kpd`,
`MTK_COMBO`/`mt76x8_wlan`. These are the vendor stack the hardware needs; disabling `ION`
because it "looks Androidy" will silently kill the display or camera. The vendor capture path
(`amzn_mt_spi_pcm`) is *not* an AFE PCM — it is an SPI driver and is required for microphones.

**Firmware loading:** the boot cmdline carries `firmware_class.path=/vendor/firmware`. On a
Linux rootfs that path does not exist, so either create the symlink `/vendor/firmware →
/lib/firmware` in the rootfs, or append `firmware_class.path=/lib/firmware` to the boot img
cmdline. The second is cleaner.

**Two configs, one tree:** `cronos-dev.config` (fbcon on, debugfs on, KALLSYMS on, serial
console at 921600 on `ttyMT0`) and `cronos-prod.config` (all of that off). Always keep a
dev-config `boot.img` in `/store/boot/` so a bad prod build is one `dd` away from diagnosis.

### 2.2 Boot image

The `boot` partition is **16 MB** and LK wants a boot image header **v0, page size 2048,
tags at `0x48000000`**, kernel load `0x40080000`, ramdisk load `0x69244e00`. Budget:

| Component | Size | Notes |
|---|---|---|
| kernel `Image` gzip | 5.5–7.0 MB (est.) | 7.4 MB on the stock Android config |
| appended DTB | ~250 KB | already patched for `mic_downmix=0`, see §2.8 |
| initramfs (xz) | 3.5–5.0 MB | **aarch64** static busybox + `slotctl` + `/init` + rescue. The kernel is 64-bit, so the initramfs is aarch64 even though the userspace is armv7 |
| **total** | **9.5–13 MB** | fits 16 MB with headroom; verify with `ls -l boot.img` |

If it does not fit: drop dropbear from the rescue initramfs (it is the single biggest item,
~1.2 MB with libs; use a static `telnetd`-over-USB-RNDIS or serial console instead), then
switch the initramfs to xz instead of gzip.

**LK constraints that shape the plan (all verified):**
- amonet's LK does **not** implement `fastboot boot`. Every test image must be *flashed*.
- The `recovery` slot **boots 32-bit kernels only**. A 64-bit kernel written there produces
  zero output and the watchdog resets to `boot`. Never put our kernel in `recovery`.
- `reboot recovery` is signalled via an **RTC spare register**, not the MISC bootloader message.
- LK counts incomplete boots in `/proc/idme/bootcount` and parks in "hacked fastboot mode"
  after enough of them — which is a *safety net*, not a recovery plan.

### 2.3 Root filesystem construction

Build **offline**, never on device. Method: `alpine-make-rootfs` (or an `apk --root` install)
inside an armv7 chroot via QEMU user emulation (`qemu-user-static` + binfmt), because `apk`
must run package triggers as armv7 code.

```sh
# host, once
sudo apt install qemu-user-static binfmt-support squashfs-tools
# or: docker run --rm --privileged multiarch/qemu-user-static --reset -p yes

# 1. base
curl -LO https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/armhf/alpine-minirootfs-3.24.2-armv7.tar.gz
mkdir -p build/root && sudo tar -xzf alpine-minirootfs-*.tar.gz -C build/root

# 2. packages (the entire list — see table below)
sudo cp /usr/bin/qemu-arm-static build/root/usr/bin/
sudo chroot build/root /sbin/apk add --no-cache \
    busybox musl openrc \
    wpa_supplicant=2.9-r* libnl3 \
    alsa-lib alsa-utils \
    mbedtls-static \
    lvgl  # if packaged; otherwise vendor the source and build -Os
# 3. overlay our tree (init scripts, daemon, firmware, modules, models)
# 4. strip, then pack
sudo mksquashfs build/root build/root.squashfs -comp xz -b 131072 -no-xattrs -all-root
```

**Deliberately excluded, with the reason and the saving:**

| Excluded | Saving | Why |
|---|---|---|
| `openssl` / `libcrypto3` / `libssl3` | **5.9 MB** | The only TLS we need is one client; mbedTLS (~600 KB) or BearSSL (~300 KB) covers it |
| `tzdata` | 441 KB | `TZ=UTC`; local time is a UI concern, computed from a fixed offset |
| `ca-certificates-bundle` | 174 KB | Pin exactly the one root (e.g. ISRG X1) the HA endpoint uses, or pin the cert itself |
| `apk-tools` on device | ~380 KB | Updates are whole-slot images; keep `apk.static` in the *initramfs* rescue env only |
| `BlueZ` | 6–10 MB | Off by default; feature bundle, see §11 |
| `shairport-sync` / AirPlay / DLNA | 3–5 MB | Feature bundle |
| Python, Node, Go runtime, ffmpeg, GStreamer, PulseAudio, PipeWire | 20–200 MB | Never |
| `udev`/`eudev` | 1–2 MB | devtmpfs + inittab; nothing hotplugs that we care about |
| fonts (freetype, fontconfig) | ~2 MB + RSS | One embedded 8×16 and one 24×32 **bitmap** font, ~40 KB total, compiled into the daemon |
| `agetty`/`login`/`shadow` | ~200 KB | No local logins; `/bin/sh` exists for the serial console only |
| `less`, `nano`, `vim`, `git`, `curl` | 3–5 MB | Not on an appliance. `wget` from busybox for diagnostics, if you must |

**Packing choice:** **squashfs, xz, 128 KB blocks, `-no-xattrs`, `-all-root`**. Not ext4 (2–4×
bigger), not erofs (kernel 4.9 has no erofs), not cramfs (no xz, 16 MB limit).

**Why not overlayfs in production:** an overlay's upper layer lives in tmpfs, so every write
to "the root filesystem" becomes unreclaimable RAM. On 996 MiB with a 30-day uptime that is a
slow leak into swap-or-OOM. Production: **rootfs read-only, `/var` and `/run` are tmpfs, and
the only persistent state is `/data` (ext4).** Keep `CONFIG_OVERLAY_FS=y` so a dev build can
use it, but ship without it.

**Estimated rootfs composition (est., measure with `du -a | sort -h`):**

| Component | Uncompressed | Notes |
|---|---|---|
| musl + busybox + baselayout | 1.8 MB | |
| cronosd + LVGL + fonts + assets | 1.2 MB | `-Os`, `-Wl,--gc-sections`, stripped |
| wpa_supplicant 2.9 + libnl | 2.0 MB | version pinnned, see §7.3 |
| alsa-lib | 0.5 MB | |
| mbedTLS | 0.6 MB | |
| kernel modules (wlan, bt, others on demand) | 4–6 MB | arm64 `.ko` |
| firmware (`WIFI_RAM_CODE*`, `mt7668_patch*`, `EEPROM_MT7668`, `TxPwrLimit`, BT fw) | 3–5 MB | from LineageOS `/vendor/firmware` |
| wake-word model (INT8 tflite-micro) | 0.4–1.0 MB | |
| tflite-micro runtime | 0.6 MB | |
| **rootfs total** | **14–18 MB → squashfs ~7–10 MB** | |

That last line is the headline number and it is aggressively better than TECHO5's 130 MB.
The difference is mostly: no Go binary (~10 MB), no OpenSSL (~6 MB), no BlueZ/AirPlay/SIP
(~10 MB), xz squashfs instead of ext4 (~2.5×), and shared libraries instead of the static
linking everything approach. **Do not quote it until `du` says so.**

### 2.4 Storage layout

| Partition | Size | Use in cronos-min |
|---|---|---|
| p1 kb, p2 dkb, p3 lk, p4 tee1, p6 tee2, p5 logo | 1/1/1/5/5/1 MB | **untouched** — kaeru LK lives in `expdb`; patching `lk` relocks the unit |
| p7 expdb | 16 MB | **untouched** — holds `cronos-kaeru.bin`, the boot logo slot |
| p8 MISC | 512 KB | untouched |
| **p9 boot** | **16 MB** | our kernel + initramfs |
| **p10 recovery** | **16 MB** | **keep TWRP** — it is the recovery path (32-bit kernels only) |
| p11 swdl | 32 MB | untouched |
| **p12 system** | **3.0 GB** | `/store`, ext4, label `cronos-store` |
| p13 cache | 256 MB | unused (reserve; optional `/cache` for logs if `/data` is unavailable) |
| p14 persist | 16 MB | **untouched** — sensor/mic calibration, `unlock_code` |
| p15 metadata | 40 MB | untouched |
| **p16 userdata** | **3.9 GB** | `/data`, ext4 |

```
/store/
  slots/a/root.squashfs        ~8 MB     (immutable)
  slots/a/manifest.json        <1 KB     (version, sha256, build id)
  slots/b/...
  slot.ptr                     4 B       (active slot + trial counter, dual-copy + checksum)
  boot/good.img                16 MB     (known-good boot.img for dd restore)
  boot/dev.img                 16 MB     (dev-config boot.img)
  boot/last-update.json        <1 KB
/data/
  wifi.conf                    0600     (SSID, psk hex — never a wpa_supplicant.conf with plaintext)
  net/  state/  cache/  media/  photos/
  log/                          capped 4 MB, rotated, RAM-buffered
```

Flash used in steady state: **~2×8 MB slots + 32 MB boot backups + <20 MB data ≈ 70 MB of
7.6 GB.** The remaining ~7 GB stays unallocated (no filesystem, no writes, no wear). That is
deliberate: **we are trading flash headroom for eMMC write endurance.**

### 2.5 Init and process model

PID 1 is `busybox init` via `/etc/inittab`. That is Alpine's normal arrangement; OpenRC runs
later as the service manager. Two variants exist in the build system:

**Variant A (ship this): busybox init + OpenRC with a 2-service runlevel**
```
::sysinit:/etc/init.d/cronos-boot          # mounts, module load, mixer init, RTC
::respawn:/usr/sbin/cronosd                # the daemon, PID ownership by inittab
::respawn:/etc/init.d/cronos-net watchdog  # or fold into the daemon
::askfirst:/bin/sh -l                      # serial console only if console= present
::ctrlaltdel:/sbin/reboot
::shutdown:/etc/init.d/cronos-shutdown
```
OpenRC's contribution: dependency ordering, `rc-status`, and the two service scripts. Cost:
~1.5–2.5 MB RSS (shell + awk during boot, ~200 KB resident afterwards) plus ~400 KB of scripts.

**Variant B (fallback if the RAM gate fails): inittab only, no OpenRC.** Drops ~2 MB RSS and
~400 KB flash. TECHO5 does exactly this and works. Keep the choice behind a build flag so the
two can be compared with `ps_mem` on the same unit.

**Every process, justified:**

| PID | Process | RSS (est.) | Why it must exist |
|---|---|---|---|
| 1 | `init` (busybox) | 0.6 MB | PID 1, reaps, respawns chronosd |
| 2 | `cronosd` | 5–8 MB | The system: UI, input, audio, wake word, network client, OTA, slot commit |
| 3 | `wpa_supplicant` | 3.5–5 MB | The vendor `mt76x8_wlan` is a cfg80211 fullmac; association needs a supplicant. Spawned and reaped by cronosd; **no D-Bus**, `-u -Dnl80211` |
| – | *(none)* | | No dhcpcd (busybox `udhcpc`, 200 KB, transient). No syslogd (in-daemon ring buffer + `/data/log`). No cron (daemon timer). No udev (devtmpfs). No D-Bus (compiled out of wpa_supplicant). No avahi (daemon speaks minimal mDNS). No ntpd daemon (`ntpd -q -p …` once at boot). No sshd by default. |

**Why not systemd, runit, s6, or a custom PID 1:** all are ~equal or worse in RSS to busybox
init for a 3-process system, and each adds flash and a second mental model. The measured
difference between Alpine-with-OpenRC and Alpine-with-busybox-init on the same hardware is a
couple of MB at most — it is not where the win is. The win is *not starting things*.

### 2.6 UI stack

**fbdev + LVGL 9, no compositor, no X, no Wayland, no GPU.**

Ground truth: the panel is `st7701s`, 960×480, **native portrait** — the framebuffer is
**480 wide × 960 tall, 32 bpp BGRA (r16 g8 b0 a24)**, `line_length` 1920, virtual 480×1920,
and `/dev/graphics/fb0` reports `smem_len = 5529600` (verified on TWRP's kernel, and on the
LineageOS kernel **on a pure Linux boot**, because `CONFIG_FREE_FB_BUFFER=y` only frees it when
a frame config with no remaining layer reference arrives — which only Android sends).

Consequences that must be designed in, not discovered:

1. **Rotate in software.** Compose the UI at 960×480 landscape, rotate 90° into the portrait
   framebuffer. A NEON row-copy rotate of 460,800 pixels costs ~2–4 ms on one A53 at 1.3 GHz.
   At a 10–20 fps dashboard that is 3–8 % of one core, and **zero at idle** because a clock
   only redraws the changed region.
2. **Double-buffer for free.** The framebuffer's virtual height is 1920, i.e. two pages. Use
   `FBIOPAN_DISPLAY` to flip between them: tear-free double buffering with **no extra
   allocation**. (LVGL's `DIRECT` mode + a custom flush callback that pans.) If panning proves
   unreliable, fall back to a 184 KB partial buffer (960×12 rows) and only ever write changed
   rectangles.
3. **No fbcon.** `CONFIG_VT` is off in these kernels, so there is no text console on the panel.
   Boot must show something from the first frame: paint a splash from the initramfs via a
   ~40-line fb helper, and hand the fb fd to cronosd.
4. **Backlight** is `/sys/class/leds/lcd-backlight` (0–255). Dim by ALS lux, not by timer alone.
5. **Touch**: `goodix-ts` (GT9xx, I²C 2-0x5d) exposes **multitouch protocol B only**
   (`ABS_MT_SLOT`, `TRACKING_ID`, `POSITION_X/Y`, `TOUCH_MAJOR`) and **no single-touch axes**,
   with coordinates in the portrait frame (raw 480×960). Read ranges with `EVIOCGABS`, turn a
   quarter turn, done. No calibration file, no firmware blob, no driver work — this is the
   least risky subsystem on the board.

**Rejected alternatives, with numbers:**

| Option | RSS | Verdict |
|---|---|---|
| LVGL 9 over fbdev | **3–6 MB total app** | ✅ chosen. LVGL's own static RAM ~2 KB, heap 48–128 KB |
| SDL2 on KMSDRM | ~5 MB app + 15–30 MB of libraries | No DRM path exists on 4.9; also 3× the flash |
| Qt EGLFS / linuxFB | 35–60 MB RSS, 25–40 MB of `.so` | 10× the RAM for a clock |
| Xorg + fbdev + tinywm | 12–20 MB RSS, 30–45 MB flash | Never. Pays for a protocol stack nothing needs |
| Wayland/weston | 30–60 MB | Never |
| Chromium/multimedia kiosk | 250–400 MB | Never. It alone eats a third of the device |
| Hand-rolled framebuffer + bitmap blits | 0.5–1 MB | Viable *if* LVGL's footprint disappoints; you lose widgets and re-do font/antialiasing work |

### 2.7 Audio stack

One ALSA card, `mt-snd-card`, 24 PCM devices. Only two matter, with **verified** formats:

| Node | Role | Format |
|---|---|---|
| `pcmC0D22c` | `TLV320AIC3101 Capture` | **S24_3LE, 4 ch, 16 000 Hz**, ring 320×8 |
| `pcmC0D23p` | `MAX98396_Playback` | **S16_LE, 2 ch, 48 000 Hz**, ring 768×4 |

Capture channel map (verified by tone injection): **ch0 = mic, ch1 = mic (or the *second* mic
if the DTB downmix property is removed), ch2 = playback loopback L, ch3 = playback loopback R.**

Design rules, each of which is a bug if violated:

1. **Hold `/dev/snd/pcmC0D1c` open, unconfigured, for the entire lifetime of the daemon.**
   If no other AFE stream is open, the DL1 driver puts its ring in AFE SRAM and the first
   `copy_from_user` faults at `ffffff8009eb0004` → **kernel panic and reboot**, reproducible
   every time (6/6). With another PCM held open it takes the DRAM path and plays cleanly.
   This is not a workaround that can be deferred; it is a startup invariant.
2. **Never write `Ext_Speaker_Amp_Switch`.** On cronos that control drives `gpio-392`, the
   MAX98396's reset. Toggling it resets the amp and wipes the codec driver's probe-time
   register setup, which the driver never repeats (`init_done`), so the speaker is silent until
   reboot. (On the 1st-gen board the same control is a normal amp enable — do not copy that
   logic here.)
3. **Clear `Speaker Safe Mode A` at speaker init.** The driver leaves it at 1, which caps output
   by ~30 dB (measured: −47 dBFS vs −15 dBFS at the mic for the same tone).
4. **Route the capture inputs.** A bare Linux boot leaves `ADC_A MICPGA Volume Ctrl = 0` and no
   input selected, so capture reads −95 dBFS. Set `ADC_A Left Ip Select = ADC_A DIF1_L` and
   `… Right … DIF1_R`, `ADC_A MICPGA Volume Ctrl = 40`. Floor then ≈ −70 dBFS.
5. **Detect the amp variant before touching mixer controls.** `cat /sys/bus/i2c/devices/2-*/name`
   → `max98396` (0x3d) or `tas5805m` (0x2c). Keep **two mixer init tables**, selected at boot;
   the TAS5805M's control names differ. Getting this wrong means "no sound on some units",
   which is the #1 field-support risk of the whole project.
6. **Mix in-process.** The daemon owns both PCMs exclusively, so it does its own mixing, gain
   and ducking. **No dmix, no dsnoop, no PulseAudio, no PipeWire** — those exist to share
   devices between processes, and there is exactly one process.
7. **AEC**: the loopback pair on ch2/ch3 makes software AEC possible without the vendor HAL.
   Use **speexdsp** (`speex_echo_state`) at 16 kHz — ~150 KB flash, 2–4 % of one core. WebRTC
   AEC3 is 3–4× the CPU and ~1 MB. Do *not* attempt a fixed beamformer: there are only two
   mics and Amazon's own FBF coefficients are per-unit tuned in `coefs_FBF.cfg`. Duck playback
   during listening as the cheap fallback.
8. **Playback sample rate**: prefer 48 kHz from the server. If you must resample, use
   speexdsp's resampler once, in-process, and never a `plug`/`rate` ALSA chain.
9. **Mic downmix**: patch the appended DTB to delete `amzn,mic-downmix` (no kernel rebuild
   needed) so ch0/ch1 carry the two real microphones instead of their average.

### 2.8 Device tree

Do not fork the DTS for cosmetics. `tools/linux/patch-dtb.py`-style patching of the **appended
DTB inside the boot image** is enough for every change we need:

- delete `amzn,mic-downmix` → two real mic channels;
- append `firmware_class.path=/lib/firmware` to the cmdline (or symlink `/vendor/firmware`);
- keep `lcm=1-st7701s_wsvga_dsi_vdo_cronos_st_truly`, `vram=7340032`, `fps=5964` — LK supplies
  these, do not fight them.

Everything else (GT9xx at 2-0x5d, AIC3101 at 0-0x18, MAX98396 at 2-0x3d / TAS5805M at 2-0x2c,
privacy GPIOs 384/405, buttons 393/394/404, lens cover 499, `mtk-kpd`, `alsps` at 0-0x44) is
already in the eleven appended DTBs. **Verify by reading them, do not assume.**

### 2.9 Networking

- **Wi-Fi**: vendor fullmac `mt76x8_wlan.ko` (cfg80211) + `wpa_supplicant` **pinned to 2.9**.
  Version 2.11 advertises RSN capabilities 0x000c (16 PTKSA replay counters) in message 2/4;
  the vendor driver generates its own RSN IE with capabilities 0, hostapd compares and
  deauthenticates with reason 2, which the supplicant reports as a wrong key. 2.9 associates
  and completes the handshake. **This is a hard version pin, documented in the build.** Pin it
  in `apk` with `wpa_supplicant=2.9-r*` and add a build-time assertion so a 3.24 repo bump
  cannot silently break it.
- **Firmware**: `WIFI_RAM_CODE_MT7668.bin`, `WIFI_RAM_CODE2_SDIO_MT7668.bin`,
  `mt7668_patch_e2_hdr.bin`, `EEPROM_MT7668.bin`, `TxPwrLimit_MT76x8.dat`, `wifi.cfg` — copied
  from the LineageOS `/vendor/firmware`. No WMT daemon involved.
- **BSS selection**: the firmware picks the BSS by SSID on any band and ignores the requested
  BSSID. Never pin `bssid=`, it makes joins fail.
- **IP**: busybox `udhcpc` with a 20-line script that writes `/etc/resolv.conf` into tmpfs and
  signals cronosd. No dhcpcd, no NetworkManager, no `netifrc`.
- **Reconnect**: in cronosd, an exponential-backoff state machine driven by `cfg80211` events
  from the wpa_supplicant control socket (`/run/wpa_supplicant/wlan0`), not a polling loop.
  Re-scan only after 3 failed attempts; log a single line, not one per attempt.
- **mDNS**: ~200 lines in cronosd to answer one `_esphomelib._tcp` PTR/SRV/TXT query. No Avahi.
- **DNS/TLS**: mbedTLS client with a pinned root. HTTP only on the LAN if you accept it.

### 2.10 Updates, A/B, and rollback

**Model: two slot directories each holding one squashfs image, on one ext4 partition.**

Why directories, not partitions: the eMMC GPT is fixed under amonet's LK, and repartitioning is
not reversible with a single fastboot command. Two images on an existing 3 GB partition give
all of A/B's benefits with none of the partitioning risk.

```
slot.ptr (in /store, 4 KB, two copies + CRC):
   struct { u32 magic; u32 active; u32 trials_left; u32 crc32; }  ×2
```

Update procedure (all inside cronosd, no updater daemon):

1. Fetch a manifest; verify **Ed25519** signature against the compiled-in public key. Reject
   anything unsigned. No JSON parsers: a strict 400-line tokenizer, ~8 KB of code.
2. Check the target slot's free space and the image's sha256, then **stream-decompress-and-write
   to the inactive slot** (`/store/slots/<inactive>/root.squashfs.new`), `fsync`, `rename`.
3. Mount the new image loop-read-only and validate: does `sbin/init` exist, does the ELF
   interpreter resolve, is the version file what the manifest promised? (`/bin/true` under
   `chroot` is a stronger check and costs one `fork`.)
4. Flip `slot.ptr`: `active = new`, `trials_left = 3`. `fsync` both copies.
5. Return "installed, reboot to apply". **Never reboot on your own** unless the user opted in.

Boot logic in the initramfs:

```
read slot.ptr → if trials_left == 0 → mark this slot bad, switch to the other, reboot once
else trials_left-- ; write back ; mount and continue
```
Commit (from cronosd, 5 minutes after a successful boot with no restart):
```
trials_left = 0 is wrong; write trials_left = 0xFF (committed) for the active slot
```
On the third boot without a commit: switch slots. If neither slot commits: **stay in the
initramfs as the rescue environment** — serial console on `ttyMT0` at 921600, USB RNDIS + a
`telnetd` on 10.0.0.2, `fastboot` reachable by button combo. The unit is always reachable.

**Kernel updates are not slots.** A new `boot.img` is written to `/store/boot/<ver>.img`,
the current one copied to `/store/boot/good.img`, and applied either by cronosd (`dd` to
`/dev/block/mmcblk0p9` from within the *running* old kernel — the risk is a power cut mid-write,
so only do it with battery-charged mains and a verified write) or from TWRP. Keeping
`good.img` on `/store` means TWRP can always restore without a PC. **Never flash our kernel to
`recovery`.**

### 2.11 Security and privacy

| Threat | Control |
|---|---|
| Amazon cloud / telemetry | Nothing Amazon runs. No Alexa, no accounts, no DNS names resolved unless the user configures a server. Verify with `tcpdump` on a fresh boot: expect DHCP, NTP only if enabled, and one outbound socket to the configured HA host |
| Remote shell | `sshd`/`dropbear` **absent from the prod rootfs**. Console is USB serial (`ttyMT0`, pad TP55) or USB RNDIS. An opt-in bundle adds dropbear with key-only auth |
| Malicious update | Ed25519-signed manifest, compiled-in pubkey, no key on disk, no rollback to a downgrade without an explicit `--allow-downgrade` |
| Persistence of compromise | Rootfs is **read-only squashfs**; a reboot is a factory reset of everything except `/data` |
| Credential leak | Wi-Fi PSK stored as a 32-byte hex in `/data/wifi.conf`, mode 0600, never in a config file the supplicant reads directly (cronosd hands it over the control socket with `-W`) |
| Network attack surface | No listening TCP ports in the default image. The HA connection is **outbound** |
| Kernel CVE exposure | 4.9 is EOL and unpatchable at scale — mitigated by *not running services*: no shell, no parser reachable from the network except the OTA manifest decoder and the voice stream |
| Physical | The mic latch is hardware and cannot be defeated by software; the camera has a mechanical cover reported on `gpio-499` — reflect its state in the UI honestly |
| Secrets at rest | No disk encryption (a 4.9 vendor kernel with no secure boot cannot protect keys from someone holding the eMMC). Say so out loud rather than implying otherwise |

Optional hardening (evaluate, do not assume): `dm-verity` on the squashfs slot verifies the
rootfs against a hash tree at every read — good against flash corruption, irrelevant against an
attacker who can rewrite both the image and the hash. Cheap to enable (`CONFIG_DM_VERITY`),
costs ~1 % CPU on reads, and turns silent bit-rot into a clean boot failure that triggers the
existing rollback. Worth it; not free.

---

## 3. Concrete targets

### 3.1 Flash (the hard numbers)

| Artifact | Target | Gate (fail the build) |
|---|---|---|
| `boot.img` | 9.5–13 MB | > 15.5 MB |
| kernel `Image.gz` | 5.5–7.0 MB | > 7.6 MB |
| initramfs | 3.5–5.0 MB | > 6.5 MB |
| `root.squashfs` | **7–10 MB** | > 20 MB |
| per-slot total | **< 20 MB** | > 26 MB |
| resident flash (2 slots + 2 boot backups + data) | < 90 MB | > 150 MB |
| `/data` steady state | < 20 MB | > 100 MB |

### 3.2 RAM (measure with `ps_mem`, `cat /proc/meminfo`, and `slabtop`)

| Consumer | Target (MiB) | Notes |
|---|---|---|
| Kernel: text+rodata+data | 8–12 | arm64 4.9 trimmed; 22 MB uncompressed image is normal |
| Kernel: slab + page tables + DMA pools | 10–16 | vendor drivers dominate; measure with `slabtop -s c` |
| `init` + inittab parents | 0.6 | |
| `cronosd` incl. framebuffer (3.7 MB), LVGL (0.13 MB), audio rings (32 KB), TLS | 5–8 | |
| `wpa_supplicant` | 3.5–5 | |
| tmpfs `/var` + `/run` + `/tmp` | 2–5 | capped with `size=` mount options |
| page cache (squashfs, modules, firmware) | 4–8 | reclaimable; does not count against the gate |
| **Total used at idle** | **55–85** | **gate: < 100 MiB. Stretch: 60.** |

That leaves 900+ MiB free on a 996 MiB board. **zram is therefore not used by default** —
with a <100 MiB working set it would compress nothing worth compressing and cost CPU and
latency. Keep `CONFIG_ZRAM` compiled in and offer a 256 MiB `lz4` device behind a build flag
for the heavy bundle (camera + BlueZ + media). Never swap to eMMC: it is the fastest way to
wear out an 8 GB part with no trim budget.

**How to verify honestly:** boot, wait 10 minutes idle, then
`free -m`, `ps_mem -s`, `slabtop -s c | head -20`, `cat /proc/vmstat | grep -E 'pgalloc|pgfree'`,
and a 24-hour `MemAvailable` trace into a 64 KB ring buffer. "Idle RAM" claims without a 24-hour
trace are guesses.

### 3.3 Power and thermal

- **cpufreq**: the vendor `mt_cpufreq` driver exists in 4.9. Governor `schedutil` (if the vendor
  kernel has it) or `ondemand`; cap max to 1.1–1.2 GHz for always-on use — the thermal headroom
  is worth more than the last 100 MHz. Verify `scaling_available_frequencies` and
  `scaling_available_governors` first; do not assume a governor exists.
- **cpuidle**: MTK's `mt_idle`/`spm` drivers exist. Confirm the idle states actually engage
  (`cat /sys/devices/system/cpu/cpu0/cpuidle/state*/usage`) — a mis-set `CONFIG_MTK_*` can
  leave the SoC in WFI only, which is the difference between 0.4 W and 1.5 W at idle.
- **Suspend**: **unverified and not on the critical path.** A voice satellite wants CPU idle
  plus a dark screen, not S3; wake latency and WoWLAN reliability decide whether `echo mem >
  /sys/power/state` is usable at all. Test it in month 2, do not design around it. If it works,
  use it only for the "clock-only, nobody home" profile.
- **Screen off**: backlight 0 (`/sys/class/leds/lcd-backlight`) plus a frozen UI; keep the panel
  scanning out a black frame. Panel power-down via the `lcm` driver is a stretch goal.
- **Thermal**: read `/sys/class/thermal/thermal_zone*/temp` and the trip points. An always-on
  5.5" panel in a sealed plastic shell with no fan will soak; expect 45–55 °C and cap the CPU.
  Log `thermal_zone` alongside `MemAvailable` in the 24-hour trace.
- **Wi-Fi power**: the vendor driver may expose `power_save`; enabling it costs latency and
  buys ~100 mW. Measure rather than believe.

---

## 4. Critical components, ranked by work required

| # | Component | State today | Work for cronos-min | Risk |
|---|---|---|---|---|
| 1 | **Kernel config surgery + boot.img packaging** | TECHO5 has a working recipe | Rebuild with squashfs/loop/devtmpfs/BT; produce dev+prod configs; script the boot image. **1–2 weeks** | Medium. Every removed driver must be boot-tested; a silently missing `ION`/`M4U` kills display or camera |
| 2 | **Audio init across amp variants** | Fully characterised by TECHO5 (incl. two panics and a 30 dB trap) | Port the sequence to C; two mixer tables; safe-mode clear; DL1 hold; ADC routing. **1–2 weeks** | **High.** Wrong variant = silent unit. Must be detected, not assumed |
| 3 | **fbdev display + rotation + double buffer** | Verified working; `fbprobe` paints the panel from TWRP | LVGL fbdev driver + 90° NEON rotate + `FBIOPAN_DISPLAY` + splash from initramfs. **1 week** | Medium. Falls over if anything ever sends an Android frame config — so never load Android's display HAL, and re-map the fb defensively |
| 4 | **Wi-Fi (MT7668)** | Solved, with a **hard wpa_supplicant 2.9 pin** | Vendor module build + firmware + pin + backoff state machine. **3–5 days** | Medium-low. The version pin is a foot-gun that must be asserted in CI |
| 5 | **A/B slots, signed OTA, rollback, rescue** | TECHO5's `slotctl` proves the model | Rewrite in C (~1,500 lines): slot.ptr, trial counting, Ed25519 verify, hash check, mount-validate, commit. **1–2 weeks** | Medium. This is the code that bricks units if it is wrong — fuzz it |
| 6 | **Bluetooth** | **Not possible today**: neither kernel has `CONFIG_BT`; the vendor driver only creates `/dev/stpbt` (raw H4), no HCI | Rebuild with `CONFIG_BT`+`HCI_VHCI`, write `btbridge` (~400 lines: stpbt ↔ /dev/vhci), then BlueZ. **2–3 weeks** | Medium-high; adds ~6–10 MB RSS and a large attack surface for marginal value. **Ship it off** |
| 7 | **Camera** | Works in TECHO5 by driving ISP registers; no V4L2 pipeline | Feature-gated snapshot path only; module loaded on demand; 2 MP YUV422 ≈ 4 MB/frame so **one buffer, one frame, never streaming**. **3–6 weeks, uncertain** | **High.** Highest effort, lowest payoff, and the privacy cover is on `gpio-499` |
| 8 | **Wake word + AEC** | microWakeWord (Apache-2.0, INT8) or openWakeWord (Apache-2.0 code, **NC weights**) | tflite-micro in the daemon; speexdsp AEC on the loopback channels. **2 weeks** | Medium-low. Fits comfortably; licensing needs care if commercial |
| 9 | **ALS, buttons, mute latch, lens cover** | Documented; ALS needs the vendor sysfs switch, not IIO | ~300 lines: `echo <ns> > /sys/class/misc/m_alsps_misc/alsdelay`, `echo 1 > …/alsactive`; `KEY_POWER` from `gpio-privacy-button` means "sleep the screen", not "power off"; latch state from `gpio-privacy/state` | Low |
| 10 | **Power/thermal tuning** | Vendor PM present, suspend unverified | Governor + cap, idle-state verification, screen-off path, 24 h trace | Medium. Easy to claim, hard to prove |

**Read this before writing a line of driver code:** the eleven DTBs already in the kernel and
the mixer/driver behaviour above were paid for in panics and silent speakers by the TECHO5
author. Re-deriving them costs weeks. Port the findings, cite the sources, and spend your own
time on the parts nobody has done: C daemon, squashfs slots, and the memory profile.

---

## 5. Build and deploy plan

### P0 — Host and prerequisites (~1 day)
```sh
# Host: any Linux (WSL2 works). Needs qemu-user-static + binfmt for armv7 apk triggers.
git clone https://github.com/amazon-oss/android_kernel_amazon_mt8163 -b cronos/lineage-18.1
sudo apt install gcc-aarch64-linux-gnu qemu-user-static binfmt-support \
                 squashfs-tools android-sdk-platform-tools
```
**Gate:** `file vmlinux` → `ELF 64-bit LSB ... ARM aarch64`. If it says ARM 32-bit you are on
the wrong branch and every module will be wrong.

### P1 — Kernel (`cronos-dev` config) (~3–5 days)
1. `make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- cronos_defconfig` (or start from the
   device's `/proc/config.gz` dump — **preferred**, it is guaranteed consistent with the DTBs).
2. Apply the enable/disable list from §2.1 in **three commits**: (a) enable only, (b) remove
   filesystems/networking, (c) remove debug. Bisectable.
3. Build `Image.gz` + `dtbs` + modules; record `du -h` for each.
4. Package `boot.img`: header v0, page 2048, `--kernel Image.gz --ramdisk initramfs.cpio.gz
   --cmdline "console=ttyMT0,921600n1 …"`, appended DTB, then `patch-dtb.py --no-mic-downmix`.
**Gate:** flash to `boot`, boot, and get a shell on the serial console. `dmesg` shows the panel,
`i2c` devices, `snd` card, and `mmc1` SDIO. If display is dead: check `smem_len` on
`/dev/graphics/fb0` — nonzero means the fb survived, zero means something sent a frame config.

### P2 — initramfs (~3 days)
Static busybox, `/init` in C or POSIX sh (~150 lines): mount store, read slot.ptr, trial
decrement, loop-mount squashfs, tmpfs mounts, splash paint, `switch_root`, and **delete the
initramfs files to release its ~4 MB of ramfs pages** before switching.
**Gate:** boots into a shell in the rootfs; `free -m` shows the initramfs memory gone.

### P3 — Rootfs image (~1 week)
Build as in §2.3. Then the size audit: `du -a build/root | sort -n | tail -40`, `squashfs` it,
and record the number. **Gate: `root.squashfs` < 20 MB, < 10 MB target.**

### P4 — cronosd bringup, in this order (each step is a demoable milestone) (~4–6 weeks)
1. fbdev + rotate + splash + backlight → clock on screen.
2. touch (evdev, protocol B, quarter turn) → tappable regions.
3. ALSA init (amp detect, safe-mode clear, ADC routing, DL1 hold) → beep in / beep out.
4. leanback capture + speexdsp AEC → echo-cancelled 16 kHz mono.
5. microWakeWord on that stream → wake event + LED/flash feedback.
6. TLS client to the HA/ESPHome endpoint; post-wake audio out, TTS audio in and play.
7. OTA + slot commit.
**Gate after step 3:** 10 minutes of continuous playback with no panic and no underrun. After
step 5: `ps_mem` shows the whole system under 100 MB.

### P5 — Slots and OTA (~1–2 weeks)
Two slots + `slot.ptr` + signed manifest + trial/commit + rescue. **Fuzz the slot parser and the
manifest decoder before touching hardware**; a bad `slot.ptr` is a brick-shaped bug.
**Gate:** deliberately ship a slot whose init segfaults; confirm three boots and an automatic
rollback to the good slot, unattended.

### P6 — Install on hardware (~half a day per unit)
Requires **amonet 2.x already applied** (fastboot reports `product: CRONOS`, `kaeru-version:
2.0.0`, `unlock_status: true`). If it says `product: CHECKERS`, stop: the unit is on the 1.x
layout and cronos-min will not boot.
```sh
adb reboot bootloader                     # or: vol-down while plugging in
fastboot devices                          # expect CRONOS
fastboot flash boot   boot-cronos-min.img
fastboot flash recovery recovery-twrp-cronos.img   # keep TWRP; 32-bit kernel only
fastboot continue
# then, from TWRP: format p12 as ext4 (label cronos-store), p16 as ext4,
# push the first slot + slot.ptr, and reboot.
```
**First boot checklist:** panel paints a splash, clock appears, touch responds, speaker beeps,
mic captures, Wi-Fi joins, `free -m` under 100 MB. If the display is black but the serial console
is alive, you are in the good failure mode — the framebuffer was freed or never mapped.

### P7 — Measure and tune (~1 week)
24-hour trace of `MemAvailable`, `slabtop`, `thermal_zone0/temp`, CPU idle residency, Wi-Fi
reconnect count, eMMC writes (`/sys/block/mmcblk0/stat` sectors written — a device that writes
>100 MB/day will not last a decade). Then decide: OpenRC or inittab-only, zram or not, suspend
or not. **Every one of those is a measurement, not a preference.**

### P8 — Optional bundles (continuous)
BlueZ/A2DP, camera snapshot, media playback, AirPlay — each as a separate squashfs overlay or
slot variant, each with its own measured RSS, each **off by default**. A bundle that pushes idle
RAM past 120 MB does not ship.

**Total: roughly 10–14 weeks of focused work for one experienced embedded developer**, of which
weeks 1–3 are the risky ones (kernel + audio) because they are the parts where being wrong is
silent.

---

## 6. Risks and known weak points

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | **Bricking the unit** — most cronos units lack BROM USBDL access, so a bad write to preloader/LK/TEE is unrecoverable | **Fatal** | Never write p3/p7/tee1/tee2/preloader/expdb. Only `boot`, `recovery`, `system`, `userdata`. Keep `good.img` on `/store`. Know that TWRP + `fastbrick` recovery exists but depends on the bootloader chain being intact |
| 2 | **Anti-rollback fuses are one-way** | Fatal | Never flash firmware images of unknown provenance. Do not "update" the bootloader |
| 3 | **Display**: fb survives only because a Linux boot never sends an Android frame config | High | Never load Android display/userspace. Re-map the fb after any suspend/resume. Keep a serial console path so a black panel is diagnosable. The vendor `disp_mgr` overlay path **does not latch** — do not spend a month on it |
| 4 | **Audio variant silence** (MAX98396 vs TAS5805M) | High | Detect by I²C name at boot; two mixer tables; a boot-time self-test that plays 50 ms and reads the amp's fault/VI-sense registers if available |
| 5 | **The 30 dB trap** (`Speaker Safe Mode A`) and the **amp reset trap** (`Ext_Speaker_Amp_Switch`) | High | Both are one-liners in the init sequence and both are catastrophic if missed. Put them in a unit test that greps the C source for the forbidden control name |
| 6 | **DL1 SRAM panic** | High | Hold `pcmC0D1c` open from process start to exit, including across suspend if implemented. Never open playback before the hold |
| 7 | **Mic mute latch cannot be released in software** | Medium | UI reflects `gpio-privacy/state`; never offer a software unmute. After unmute, capture must be reopened |
| 8 | **wpa_supplicant version drift** | Medium | `apk` version pin + build-time assert + a CI boot test that associates to a known AP |
| 9 | **Bluetooth is not "free"**: needs a kernel rebuild, an HCI bridge, and BlueZ | Medium | Feature-gated, off by default, own slot variant |
| 10 | **Camera is the least-solid subsystem** | Medium | Off by default, module on demand, single buffer, never on the boot path |
| 11 | **Kernel 4.9 is EOL** — no upstream security fixes, no new mitigations | High (unfixable) | Minimise reachable attack surface: no listeners, signed OTA, one TLS peer, no shell. Be honest that this is the price of the hardware |
| 12 | **eMMC wear on an 8 GB part** | Medium | Read-only root, `/var` in tmpfs, logs capped at 4 MB in RAM, `noatime`, `commit=600`, no swap on eMMC, and a sectors-written counter in the health report |
| 13 | **No suspend, or suspend that hangs** | Medium | Not on the critical path; test in month 2; CPU idle + dark screen is the fallback and is genuinely enough for a satellite |
| 14 | **Thermals in a sealed shell** | Medium | Cap the CPU, monitor trip points, degrade the UI frame rate before throttling the audio path |
| 15 | **`boot` is only 16 MB** | Medium | Kernel `-Os` + xz initramfs + no dropbear in rescue; enforce with a build gate |
| 16 | **Vendor blobs**: `mt76x8_wlan.ko` and firmware are GPL-with-blobs | Low-medium | Source is mirrored publicly; firmware redistribution terms need reading before shipping a binary release. This is a licensing risk, not a technical one |

---

## 7. Making it smaller than TECHO5 (concrete, ordered by payoff)

TECHO5 is already the right shape. These are the deltas, ranked by measured-or-estimated
saving, each independently shippable.

| # | Change | Flash saving (est.) | RAM saving (est.) | Cost/risk |
|---|---|---|---|---|
| 1 | **C daemon instead of Go** | 8–12 MB (Go runtime + binary) | **8–15 MB** | 4–6 weeks of dev time; the biggest single win |
| 2 | **squashfs (xz) slots instead of ext4 directories** | 2–2.5× on the rootfs → 60–90 MB | 0 (slightly negative: decompression page cache) | Needs `CONFIG_SQUASHFS` (TECHO5's kernel lacks it). Boot is ~0.5 s slower per MB |
| 3 | **mbedTLS/BearSSL instead of OpenSSL** | 5–6 MB | 1–2 MB | Re-implement TLS setup; ~2 days |
| 4 | **Drop BlueZ/AirPlay/DLNA/SIP from the base image** | 10–15 MB | 8–12 MB | Feature bundles only |
| 5 | **No OpenRC** (inittab only) | 0.4 MB | 1.5–2.5 MB | Lose `rc-status`/deps; ~1 day |
| 6 | **No apk-tools on device** (keep it in the initramfs) | 0.4 MB | ~0 | Updates are whole slots anyway; ~1 hour |
| 7 | **Trim BusyBox applets** (custom `defconfig`) | 0.3–0.4 MB | ~0 | Loses `setup-*` helpers; ~2 hours |
| 8 | **Bitmap fonts instead of freetype/fontconfig** | ~2 MB | 1–2 MB | Ugly text or hand-hinted bitmaps; ~1 day |
| 9 | **No tzdata / no CA bundle** | 0.6 MB | ~0 | Pinned cert + fixed offset; ~1 hour |
| 10 | **Camera/BT/ALS as on-demand modules** | 0 in flash | 2–5 MB (not resident until used) | Modules, not built-ins; ~2 days |
| 11 | **`-Os` + `--gc-sections` + `-Wl,-z,now` + stripped, no C++ exceptions/RTTI** | 15–25 % of our own code | small | Build flags; ~2 hours |
| 12 | **`dm-verity` + a hash tree** (adds ~0.5 %) | +0.1 MB | ~0 | Turns bit-rot into a clean rollback; ~2 days |

**Things that look like savings and are not:**
- **UPX** on the daemon: halves flash, costs a full decompression into RAM at exec and breaks
  page sharing. Never.
- **Static linking everything**: bigger total flash, and worse — no page sharing between
  processes, so RSS goes *up*. Link statically only busybox (initramfs) and, arguably, cronosd.
- **zram on a 5 % working set**: costs CPU and latency to compress nothing.
- **A 32-bit kernel**: you would gain ~10 % kernel text but lose the vendor modules and the
  boot chain. Not worth it.
- **Disabling `ION`/`M4U`/`MTK_CMDQ`**: they look Android-shaped, but the vendor display and
  camera paths allocate through them. Try it in a `dev` build only.

---

## 8. What realistically fits

| Feature | Fits? | Incremental cost | Notes |
|---|---|---|---|
| Clock / status dashboard with ALS-driven dimming | ✅ | <1 MB RAM, ~0 flash | The baseline UI |
| Home Assistant voice satellite (ESPHome protocol) | ✅ | in-daemon | The primary use case; **outbound connection only** |
| On-device wake word | ✅ | 1.5–3 MB RAM, 1–2 MB flash | microWakeWord INT8 + tflite-micro; ~3–6 % of one core |
| Software AEC (so it can hear you while talking) | ✅ | 150 KB flash, 2–4 % core | speexdsp on the loopback channels |
| Full-screen touch UI with a few screens | ✅ | <1 MB | LVGL over fbdev |
| Local timers, alarms, notifications | ✅ | ~0 | In-daemon |
| Media playback (MP3/AAC/FLAC, web radio) | ✅ | 1–3 MB flash, 2–4 MB RAM | minimp3 (~100 KB) or libmad; no GStreamer, no ffmpeg |
| Bluetooth speaker (A2DP sink) | ⚠️ | +6–10 MB RAM, ~2 MB flash | Needs the kernel `CONFIG_BT` rebuild + `btbridge`. Works, costs a lot; ship off |
| Bluetooth HID remote / BLE sensors | ⚠️ | +4–6 MB | Same kernel work, less payoff |
| Camera snapshot / motion JPEG | ⚠️ | 4 MB/frame buffer, ~1 MB flash | Vendor ISP registers; no V4L2 pipeline; no hw encode. Off by default |
| AirPlay / DLNA receiver | ⚠️ | +3–5 MB flash, 2–4 MB RAM | shairport-sync; nice-to-have, not core |
| SIP intercom | ❌ | +8–15 MB | pjsip is enormous; use the HA voice path instead |
| Local STT (whisper.cpp / Vosk) | ❌ | 150–400 MB RAM, 40–200 MB flash | Vosk small = ~300 MB RSS. On a 996 MB board that is the whole device. **Server-side STT only** |
| Local TTS (Piper) | ❌ | 60–150 MB RAM + 20–60 MB voices | Server-side TTS, play the audio |
| Video playback | ❌ | — | No mainline DRM, no usable VPU userspace in Linux; the Mali T720 has no working KMS path. Do not promise it |
| Web browser | ❌ | 200–400 MB | Never |
| Any Python/Node/Java runtime | ❌ | 20–80 MB each | Never |

**Recommended default feature set:** clock/dashboard + HA voice satellite + wake word + AEC +
timers + MP3/web-radio playback + ALS dimming + mute-state display. That is a genuinely better
device than the stock one: faster, private, offline-capable, and using **~6–8 % of the RAM and
~2 % of the flash that Fire OS 7 does**.

---

## 9. Repository layout

```
cronos-min/
  kernel/
    cronos-dev.config            # fbcon, debugfs, kallsyms, serial console
    cronos-prod.config           # everything debuggy off
    build.sh                     # Image.gz, dtbs, modules, boot.img, patch-dtb
    patch-dtb.py                 # --no-mic-downmix, --set PATH:NAME:SPEC, --list
  initramfs/
    init                         # ~150 lines: slots, mounts, splash, switch_root
    busybox.config
  rootfs/
    packages.txt                 # THE authoritative list; every add needs a RAM/flash note
    overlay/
      etc/inittab
      etc/init.d/cronos-boot     # modules, mixer, RTC, hostname
      etc/init.d/cronos-net
      etc/wpa_supplicant.conf    # template only; no PSK on disk
      etc/asound.conf            # nothing but `defaults.pcm.rate_converter`
      usr/lib/firmware/...       # MT7668 wifi/BT blobs
  src/cronosd/
    main.c  ui/  input/  audio/  vw/  net/  ota/  slot/  hw/
    ui/assets/                   # bitmap fonts, icons (≤40 KB total)
    models/                      # INT8 wake-word tflite, ≤1 MB
  tools/
    mkrootfs.sh  mkinitramfs.sh  mkbootimg.sh  deploy.sh  measure.sh
  docs/
    this file, plus: audio-init.md, slot-format.md, pinning.md
```

`packages.txt` is a contract: **a line may only be added with a one-line justification and a
measured flash/RSS delta.** That single rule is what keeps a project like this from creeping
back to 130 MB.

## 10. Acceptance tests

Ship a build only when all of these pass on a real unit:

1. **Boot**: cold boot to interactive UI in < 15 s; 10 consecutive cold boots, no watchdog resets.
2. **RAM**: 24-hour idle trace, max `MemUsed` < 100 MiB, no upward slope (leak = fail).
3. **Flash**: `root.squashfs` < 20 MB, `boot.img` < 15.5 MB, measured by the build, not by hand.
4. **Display**: 8 hours of a clock at 1 fps with no tearing, no `EPERM`/`EINVAL` on fb ioctls.
5. **Touch**: all four corners map correctly through the rotation; no stuck slots after 1,000 taps.
6. **Audio out**: 1 hour of playback, no panic (DL1 hold proven), no underruns, on **both** amp
   variants if you can get two units.
7. **Audio in**: −70 dBFS floor, wake word detected at 3 m, AEC reduces the device's own output
   by > 20 dB.
8. **Network**: 100 power cycles of the AP, reconnects unattended every time; < 5 s recovery.
9. **OTA**: a broken slot rolls back unattended after 3 boots; a good slot commits after 5 min;
   an unsigned manifest is rejected; a manifest with a wrong hash is rejected and the slot is
   left untouched.
10. **Rescue**: with both slots marked bad, the initramfs comes up reachable over serial/USB.
11. **Privacy**: `tcpdump` on a fresh boot shows no traffic except DHCP/NTP/the configured server.
    `ss -tlnp` shows no listening ports.
12. **Endurance**: 30 days unattended; `mmcblk0` sectors-written < 3 GB total; no thermal trip.

---

## 11. What is implemented in this repository

The buildable half of this document. Host-tested, not device-tested.

| Path | What it does | Verified how |
|---|---|---|
| `tools/mkbootimg.py` | pack/unpack boot image v0 with the cronos addresses; refuses anything over 16 MB | byte-offset assertions against the AOSP v0 layout, round-trip, partition gate |
| `tools/fdt.py`, `tools/patch-dtb.py` | parse/edit a DTB; drop `amzn,mic-downmix`; find an appended DTB inside a kernel `Image` | round-trip plus a real `dtc`-produced blob |
| `tools/mkcpio.py` | deterministic newc cpio (hardlinks preserved) | determinism, hardlink, symlink, mode tests |
| `tools/kconfig.py` | apply fragments in place, assert the result | idempotence, ordering, string values, failing assertions |
| `src/slotctl/slotctl.c` | the A/B pointer: 16-byte dual records, CRC32, atomic rename, trial/commit/revert | 36 checks over every transition incl. corruption and double failure |
| `initramfs/init` | slot choice, squashfs mount, tmpfs `/var` `/run` `/tmp`, `/data`, `switch_root`, rescue shell | shell syntax only; needs the device |
| `kernel/*.fragment` | base (safe), trim (opt-in), dev (debug) config deltas + assertions | applied and asserted against a sample config |
| `tools/mkinitramfs.sh`, `kernel/build.sh` | assemble the initramfs, build the kernel and boot.img | architecture guard and `bash -n`; the kernel build itself is unproven |
| `tools/measure.sh` | the RAM/flash gate, on the device | not run on hardware |

Two facts discovered while building these that change the plan:

1. **The initramfs must be aarch64.** It runs on the 64-bit kernel, so an armv7
   busybox there fails with `Exec format error` *before any console exists* —
   the least diagnosable failure mode on the board. The rootfs stays armv7.
2. **The slot record is 16 bytes, not 24.** Packed as `magic[4]`, four bytes of
   state, `seq`, `crc`; the checksum covers exactly `offsetof(crc)`. An earlier
   draft checksummed 20 bytes of a 16-byte struct, which reads adjacent memory
   and produces a record that seals but never validates.

## 12. Sources

Ground truth for this board, read in full:
- TECHO5 `docs/hardware.md` — partitions, boot image layout, panel/fb behaviour, audio PCM nodes
  and channel map, DL1 SRAM panic, safe-mode cap, amp-reset trap, mute latch, ALS, touch, amonet
  constraints, mainline status: <https://raw.githubusercontent.com/HuskerMinion/techo5/main/docs/hardware.md>
- TECHO5 `docs/overview.md`, `tools/linux/README.md` — Alpine armv7 build via QEMU binfmt, two
  slots on the userdata/system partition, busybox init, `slotctl` trial/commit, 130 MB slots,
  7.4 MB kernel + ~8 MB initramfs budget: <https://github.com/HuskerMinion/techo5>,
  <https://raw.githubusercontent.com/HuskerMinion/techo5/main/tools/linux/README.md>
- amonet-cronos (unlock, 2.x layout, brick risk, no BROM USBDL on most units):
  <https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-show-5-2nd-gen-2021-cronos.4772596/>
- LineageOS 18.1 cronos kernel tree (4.9.337):
  <https://github.com/amazon-oss/android_kernel_amazon_mt8163> (branch `cronos/lineage-18.1`)
- MT8163 upstream status (RFC only, PSCI/TF-A blocker, no DRM/audio/PM):
  <https://lkml.rescloud.iu.edu/2610.2/05391.html>,
  <https://lkml.rescloud.iu.edu/2610.2/05399.html>,
  <https://github.com/bengris32/linux-mtk>
- Mainline `mediatek-drm` has no MT8163 MMSYS data:
  <https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/mediatek/mtk_drm_drv.c>
- MT7668 not supported by mainline `mt76`:
  <https://wireless.docs.kernel.org/en/en/users/drivers/mediatek.html>,
  <https://github.com/CoreELEC/MT7668>
- Alpine armhf sizing (3 MB minirootfs gz, busybox 794 KiB, musl 650 KiB, openssl 5.9 MB,
  tzdata 441 KiB): <https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/armhf/>,
  <https://github.com/docker-library/repo-info/blob/master/repos/redis/local/alpine.md>
- LVGL requirements (2 KB static RAM, 48–128 KB heap):
  <https://lvgl.io/docs/open/9.4/intro/introduction/requirements>,
  <https://lvgl.io/docs/open/9.5/integration/embedded_linux/drivers/fbdev>
- Wake word footprints (openWakeWord 15–20 models/core on a Pi 3, ~50–100 MB with Python/ONNX;
  microWakeWord INT8; Porcupine ~1 MB but closed and losing its free tier):
  <https://github.com/dscripka/openWakeWord>, <https://github.com/kahrendt/microWakeWord>,
  <https://picovoice.ai/docs/faq/porcupine/>
- Vosk small model = ~300 MB runtime (why local STT is out): <https://alphacephei.com/vosk/models>
- HA voice satellite stack and its PulseAudio coupling (why we write our own ALSA client):
  <https://github.com/OHF-Voice/linux-voice-assistant>,
  <https://github.com/rhasspy/wyoming-satellite>

**Explicitly unverified in this document:** the exact camera sensor part; whether suspend-to-RAM
works on this vendor kernel; whether an amonet 2.x unit ever reports more than 996 MB; all
`(est.)` size figures; the squashfs decision's real boot-time cost. Each has a measurement gate
in §10 — run it before quoting the numbers.
