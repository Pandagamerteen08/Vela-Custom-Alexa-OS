#!/usr/bin/env python3
"""report-sizes.py — the flash budget, checked against the gates in the design.

Run after a build (`make sizes`). Every artifact is compared with its target and
its fail threshold, so a regression is visible in one line instead of three
weeks later on a device with a full partition.

Gates (from docs/design.md §3.1):
    root.squashfs  target 7-10 MB, fail > 20 MB
    boot.img       target 9.5-13 MB, fail > 15.5 MB (partition is 16 MB)
    resident flash total fail > 150 MB
"""

import os
import sys

GATES = [
    ("root.squashfs", 20 * 1024 * 1024, "slot image"),
    ("boot.img", int(15.5 * 1024 * 1024), "kernel + initramfs"),
    ("boot.img.dev", 16 * 1024 * 1024, "dev kernel (p9 hard limit)"),
    ("initramfs.cpio.gz", 5 * 1024 * 1024, "initramfs budget"),
]


def human(n):
    return f"{n / 1024 / 1024:.2f} MiB"


def main(paths):
    roots = [p for p in paths if p]
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                if name in {g[0] for g in GATES}:
                    found.append(os.path.join(dirpath, name))
    if not found:
        print("report-sizes: no artifacts yet; run `make boot` first")
        return 0

    status = 0
    print(f"{'artifact':<28} {'size':>12} {'gate':>12}  result")
    print("-" * 70)
    for gname, limit, note in GATES:
        hits = [f for f in found if os.path.basename(f) == gname]
        if not hits:
            continue
        f = max(hits, key=os.path.getsize)
        size = os.path.getsize(f)
        verdict = "ok"
        if size > limit:
            verdict = "OVER GATE"
            status = 1
        print(f"{gname:<28} {human(size):>12} {human(limit):>12}  {verdict}  ({note})")

    total = 0
    for f in found:
        if os.path.basename(f) in ("root.squashfs", "boot.img", "boot.img.dev"):
            total += os.path.getsize(f)
    print("-" * 70)
    print(f"{'resident flash (2 slots + boot backups)':<28} {human(total * 2):>12} "
          f"{human(150 * 1024 * 1024):>12}  "
          f"{'ok' if total * 2 <= 150 * 1024 * 1024 else 'OVER GATE'}")
    if total * 2 > 150 * 1024 * 1024:
        status = 1
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
