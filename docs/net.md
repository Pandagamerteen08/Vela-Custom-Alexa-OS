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
| Reconnect | `cronosd`, driven by cfg80211 events | not a polling loop |

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

Alpine 3.20 does not ship 2.9. Build it from source in the chroot with the default
config (`CONFIG_DRV_WEXT` off, `CONFIG_DRV_NL80211` on), or pull the package from an
older branch for that one package.

## Failure mode 2: pinning a BSSID

**Never set `bssid=` in a network block.** The vendor firmware selects the BSS by SSID
itself; pinning a BSSID makes association fail on mesh, multi-AP and band-steering
setups, and works fine on the one AP you tested with.

## Bring-up sequence

```sh
modprobe mt76x8_wlan
ip link set wlan0 up
wpa_supplicant -u -Dnl80211 -i wlan0 -c /etc/wpa_supplicant.conf -B
wpa_cli -i wlan0 status          # expect wpa_state=COMPLETED
udhcpc -i wlan0 -b -q -t 5 -T 3
```

`-u` stops the supplicant from changing the interface state behind our back;
`-Dnl80211` is required (the driver has no wext).

## Reconnect policy

In `cronosd`, an exponential-backoff state machine fed by cfg80211 events from the
control socket at `/run/wpa_supplicant/wlan0`:

- 1 s, 2 s, 4 s, 8 s, 16 s, then cap at 60 s.
- Re-scan only after **3** consecutive failures — scanning is the expensive operation
  and an AP that is off does not benefit from being scanned every second.
- **One log line per failure transition**, not per attempt. A device that logs every
  3 seconds for a week fills `/data` and tells you nothing.

## What we do not run

- No `NetworkManager`, no `connman`, no D-Bus. `wpa_supplicant` is built without D-Bus
  support, and the daemon talks to it over the control socket.
- No `avahi`: `cronosd` answers one `_esphomelib._tcp` PTR/SRV/TXT query in ~200 lines
  of mDNS so a Home Assistant instance can discover the device without configuration.
- No `hostapd`, no WPS, no WPA3. PSK only, which is what the driver does reliably.

## Measured cost

`wpa_supplicant` is 3.5–5 MB RSS and is the second-largest process on the device after
the daemon. It is not optional: a fullmac driver has no in-kernel association. If the
RAM gate is ever missed, this is the wrong place to look for savings — the answer is
the UI, not the network.
