#!/usr/bin/env python3
"""packages.py — validate rootfs/packages.txt and emit the apk command.

The project rule is that every package costs a written justification. That rule
is only real if a machine checks it, so this tool fails the build when a line has
no comment, when a line names something on the forbidden list, or when a pin is
malformed.

    ./packages.py check                     # exit 1 on any problem
    ./packages.py apk-args                  # "busybox musl ..." for apk add
    ./packages.py list                      # name, pin, why
"""

import argparse
import re
import sys

# Packages that must never appear: each one is a whole runtime or a second
# implementation of something already present. If a real need appears, the
# justification has to win on measured numbers, not on convenience.
FORBIDDEN = {
    "python3": "20 MB + interpreter RSS; the daemon is C",
    "python": "alias of python3",
    "nodejs": "40 MB; no JS anywhere on this device",
    "golang": "no Go: the daemon is C to stay at ~1.2 MB",
    "ffmpeg": "20 MB; no video decode is feasible on this SoC",
    "gstreamer": "30 MB+",
    "pulseaudio": "12 MB + a daemon; one process mixes",
    "pipewire": "same",
    "openssl": "5.9 MB for one TLS client; mbedtls is 600 KB",
    "libcrypto3": "the OpenSSL runtime we are avoiding",
    "libssl3": "the OpenSSL runtime we are avoiding",
    "udev": "devtmpfs + inittab; nothing hotplugs that we care about",
    "eudev": "same",
    "dbus": "no IPC bus for a 3-process system",
    "avahi": "~200 lines of mDNS in the daemon instead",
    "chrony": "busybox ntpd -q at boot",
    "ntpd": "busybox ntpd -q at boot",
    "dropbear": "no remote shell in the prod image (opt-in bundle only)",
    "openssh": "no remote shell in the prod image",
    "tzdata": "TZ=UTC; local time is a UI concern",
    "fontconfig": "bitmap fonts compiled into the daemon",
    "freetype": "same",
    "wayland": "no compositor",
    "weston": "no compositor",
    "xorg-server": "no X",
    "bluez": "6-10 MB; Bluetooth is an opt-in bundle",
}

LINE_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._+-]*)(?:=(?P<pin>[A-Za-z0-9._+~-]+))?\s*(?P<rest>#.*)?$")


class PkgError(Exception):
    pass


class Package:
    __slots__ = ("name", "pin", "why")

    def __init__(self, name, pin, why):
        self.name = name
        self.pin = pin
        self.why = why

    @property
    def spec(self):
        return f"{self.name}={self.pin}" if self.pin else self.name

    def __repr__(self):
        return f"<{self.spec} {self.why!r}>"


def parse(text):
    pkgs = []
    problems = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = LINE_RE.match(line)
        if not m:
            problems.append(f"line {lineno}: cannot parse {raw!r}")
            continue
        name = m.group("name")
        pin = m.group("pin")
        rest = (m.group("rest") or "").lstrip("#").strip()
        why = rest
        if not why:
            problems.append(f"line {lineno}: {name} has no justification comment "
                            "(project rule: every package states why and its cost)")
        if name in FORBIDDEN:
            problems.append(f"line {lineno}: {name} is forbidden — {FORBIDDEN[name]}")
        if pin and not re.match(r"^[0-9]", pin):
            problems.append(f"line {lineno}: pin {pin!r} for {name} should start with a digit")
        pkgs.append(Package(name, pin, why))
    names = [p.name for p in pkgs]
    for dup in {n for n in names if names.count(n) > 1}:
        problems.append(f"duplicate package: {dup}")
    return pkgs, problems


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["check", "apk-args", "list"])
    p.add_argument("--file", default="rootfs/packages.txt")
    args = p.parse_args(argv)

    try:
        with open(args.file) as f:
            pkgs, problems = parse(f.read())
    except OSError as e:
        sys.stderr.write(f"packages: {e}\n")
        return 1

    if problems:
        sys.stderr.write("packages: FAILED\n")
        for prob in problems:
            sys.stderr.write(f"  - {prob}\n")
        return 1

    if args.command == "check":
        print(f"packages: {len(pkgs)} packages, all justified")
        return 0
    if args.command == "apk-args":
        print(" ".join(p.spec for p in pkgs))
        return 0
    for pkg in pkgs:
        print(f"{pkg.spec:<28} {pkg.why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
