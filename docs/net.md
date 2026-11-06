# Wi-Fi on cronos

The most confusing subsystem on this board, because two of its failure modes look
like something else entirely.

## The stack

| Layer | What it is | Notes |
|---|---|---|
| Hardware | MT7668 over SDIO, on `mmc1` | vendor `0x037a`, func1 `0x7608`, func2 `0x7668` |
| Driver | `mt76x8_wlan.ko` (aarch64, out-of-tree) | a cfg80211 fullmac, not mac80211 |
| Firmware | `WIFI_RAM_CODE_MT7668.bin`, `WIFI_RAM_CODE2_SDIO_MT7668.bin`, `mt7668_patch_e2_hdr.bin`, `EEPROM_MT7668.bin`, `TxPwrLimit_MT76x8.dat`, `wifi.cfg` | from LineageOS `/vendor/firmware`; must land in `/lib/firmware` |
| Supplicant | `wpa_supplicant` **2.9** | see below — the version is not a preference |
| Address | busybox `udhcpc`, transient | no `dhcpcd` daemon |
| Reconnect | `velad`, driven by cfg80211 events | not a polling loop |

The boot cmdline that came with Android says `firmware_class.path=/vendor/firmware`.
On a Linux rootfs that path does not exist, so either symlink
`/vendor/firmware → /lib/firmware` in the image or put
`firmware_class.path=/lib/firmware` in `kernel/cmdline.txt` (this repository does the
latter). Symptom if you forget: the module loads, `wlan0` never appears, and dmesg has
a single `Direct firmware load for ... failed` line you will only see if you look.

## Failure mode 1: the version

**`wpa_supplicant` must be 2.9.** Version 2.10 added RSN replay-counter advertisement;
the vendor driver does not implement it, so the AP deauthenticates with **reason 2**,
which the supplicant reports as an authentication failure. It looks exactly like a wrong
passphrase, and people respond by re-typing the password, changing the AP, and eventually
reflashing. It is none of those.

If a build has 2.10+, expect this line in the log and stop:

```
wlan0: CTRL-EVENT-DISCONNECTED bssid=... reason=2
```

### Which Alpine branches still carry 2.9

Read out of the `armhf` `APKINDEX` for each branch (armhf is the rootfs
architecture), not from memory:

| Branch | `wpa_supplicant` |
|---|---|
| `v3.9` | `2.7-r5` |
| `v3.10` | `2.8-r5` |
| `v3.11`, `v3.12` | `2.9-r8` |
| `v3.13` | `2.9-r11` |
| `v3.14` | `2.9-r14` |
| **`v3.15`** | **`2.9-r17`** — newest branch with 2.9 |
| `v3.16` … `v3.21` | `2.10-r…`  (`v3.20` is `2.10-r11`) |

`2.9-r6` exists in no Alpine release at all, so a pin to it fails before `apk`
looks at the network, and `2.9-r17` only resolves against `v3.15`.

`tools/mkrootfs.sh` therefore **builds 2.9 from source in the chroot**, which is
what actually ships (`docs/BUILD.md` step 4). The config is
`rootfs/wpa-2.9.config`: PSK-only, `CONFIG_DRIVER_NL80211` (2.9's spelling —
`CONFIG_DRV_NL80211` is the newer name, and 2.9 silently ignores it, which builds
a supplicant that lists no drivers and then fails at runtime), no D-Bus, no TLS
library. The build runs the artifact and fails unless it reports `v2.9`.

The alternative, if you are in a hurry and accept the risk: add `v3.15` as an
extra repository and pin `wpa_supplicant=2.9-r17`. One line, but a five-release-old
binary (and its `libnl3`) inside a 3.20 rootfs — re-check `musl` and `libnl3`.

## Failure mode 2: pinning a BSSID

**Never set `bssid=` in a network block.** The vendor firmware selects the BSS by SSID
itself; pinning a BSSID makes association fail on mesh, multi-AP and band-steering
setups, and works fine on the one AP you tested with.

## First boot: where the credentials come from

The image ships no PSK. `/etc/wpa_supplicant.conf` is a template (control socket,
`ap_scan`, no network block) and the rootfs is a read-only squashfs, so the only
writable place for credentials is `/data`:

```
/data/wifi.conf          0600 root
    SSID=MyNetwork
    PSK=correct horse battery staple     # 8-63 character passphrase
    PSK=0f1e2d...                        # or exactly 64 hex digits
    HIDDEN=1                             # optional
```

`vela-net` is in the `default` runlevel, so it runs on every boot once
`vela-boot` has loaded `mt76x8_wlan`. It calls `/usr/sbin/vela-wifi-config`, which
validates the file and writes `/run/wpa_supplicant.conf` (0600, tmpfs — never the
image) before starting the supplicant. With no credentials it starts no
supplicant at all and prints one warning, rather than starting one that silently
never associates.

Provisioning from the host, with no console and no UI:

```sh
tools/deploy.sh wifi MyNetwork 'correct horse battery staple'
```

That pushes the file over adb, sets mode 0600 and restarts `vela-net`. The PSK
travels in a 0600 temp file that is deleted on exit, so it does not end up in
shell history or on the build host.

`--variant inittab` has no OpenRC and therefore no `vela-net`: that image comes
up with no network until `velad` exists.

## Bring-up sequence

```sh
modprobe mt76x8_wlan
ip link set wlan0 up
/usr/sbin/vela-wifi-config      # /data/wifi.conf -> /run/wpa_supplicant.conf
wpa_supplicant -u -Dnl80211 -i wlan0 -c /run/wpa_supplicant.conf -B
wpa_cli -i wlan0 status          # expect wpa_state=COMPLETED
udhcpc -i wlan0 -b -q -t 5 -T 3
```

`-u` stops the supplicant from changing the interface state behind our back;
`-Dnl80211` is required (the driver has no wext).

## Reconnect policy

In `velad`, an exponential-backoff state machine fed by cfg80211 events from the
control socket at `/run/wpa_supplicant/wlan0`:

- 1 s, 2 s, 4 s, 8 s, 16 s, then cap at 60 s.
- Re-scan only after **3** consecutive failures — scanning is the expensive operation
  and an AP that is off does not benefit from being scanned every second.
- **One log line per failure transition**, not per attempt. A device that logs every
  3 seconds for a week fills `/data` and tells you nothing.

## What we do not run

- No `NetworkManager`, no `connman`, no D-Bus. `wpa_supplicant` is built without D-Bus
  support, and the daemon talks to it over the control socket.
- No `avahi`: `velad` answers one `_esphomelib._tcp` PTR/SRV/TXT query in ~200 lines
  of mDNS so a Home Assistant instance can discover the device without configuration.
- No `hostapd`, no WPS, no WPA3. PSK only, which is what the driver does reliably.

## Measured cost

`wpa_supplicant` is 3.5–5 MB RSS and is the second-largest process on the device after
the daemon. It is not optional: a fullmac driver has no in-kernel association. If the
RAM gate is ever missed, this is the wrong place to look for savings — the answer is
the UI, not the network.
