#!/usr/bin/env python3
"""Deterministic newc (SVR4) cpio writer/reader, stdlib only.

The initramfs is a cpio archive the arm64 kernel unpacks into rootfs before
running /init, so it must be newc (the only format 4.9 always understands) and
it must be byte-reproducible: two builds of the same tree have to hash the same,
or "did my kernel change?" becomes unanswerable.

  ./mkcpio.py create initramfs/ -o initramfs.cpio
  ./mkcpio.py list initramfs.cpio
  ./mkcpio.py verify initramfs.cpio

Hardlinks are preserved (the busybox applet farm depends on it), uid/gid are
forced to 0, and mtime is SOURCE_DATE_EPOCH (default 0).
"""

import argparse
import os
import stat
import struct
import sys

MAGIC = b"070701"
HEADER_FMT = "6s8s8s8s8s8s8s8s8s8s8s8s8s8s"   # magic + 13 x 8 = 110
HEADER_LEN = 110
TRAILER = "TRAILER!!!"


class CpioError(Exception):
    pass


def _pad4(n):
    return (4 - (n % 4)) % 4


def _entry(name, mode, data, ino, nlink, mtime, rdev=0, size=None):
    name_b = name.encode() + b"\0"
    size = len(data) if size is None else size
    fields = [
        MAGIC.decode(),
        f"{ino:08x}", f"{mode:08x}", "00000000", "00000000",
        f"{nlink:08x}", f"{mtime:08x}", f"{size:08x}",
        "00000000", "00000000",
        f"{(rdev >> 8) & 0xFF:08x}", f"{(rdev & 0xFF) | ((rdev >> 12) & 0xFFF00):08x}",
        f"{len(name_b):08x}",
        "00000000",   # check: ignored by every implementation
    ]
    raw = struct.pack(HEADER_FMT, *(f.encode() for f in fields))
    if len(raw) != HEADER_LEN:
        raise CpioError("internal: bad header length")
    out = raw + name_b
    out += b"\0" * _pad4(HEADER_LEN + len(name_b))
    out += data
    out += b"\0" * _pad4(size)
    return out


def create(root, mtime=0):
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise CpioError(f"{root} is not a directory")
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        for name in [""] + dirnames + filenames:
            if name == "" and dirpath != root:
                continue
            full = os.path.join(dirpath, name) if name else dirpath
            rel = os.path.relpath(full, root)
            if rel == ".":
                rel = "."
            entries.append((rel, full))
    entries.sort(key=lambda e: (e[0] != ".", e[0]))

    out = bytearray()
    ino = 0
    seen_links = {}
    dir_children = {}
    for rel, full in entries:
        st = os.lstat(full)
        parent = os.path.dirname(rel) if rel != "." else None
        if parent is not None:
            dir_children[parent] = dir_children.get(parent, 0) + 1

    for rel, full in entries:
        st = os.lstat(full)
        ino += 1
        arcname = "." if rel == "." else "./" + rel.replace(os.sep, "/")
        perm = stat.S_IMODE(st.st_mode)

        if stat.S_ISLNK(st.st_mode):
            target = os.readlink(full)
            out += _entry(arcname, stat.S_IFLNK | 0o777, target.encode(), ino, 1, mtime)
        elif stat.S_ISDIR(st.st_mode):
            nlink = 2 + dir_children.get(rel, 0)
            out += _entry(arcname, stat.S_IFDIR | (perm or 0o755), b"", ino, nlink, mtime)
        elif stat.S_ISREG(st.st_mode):
            key = (st.st_dev, st.st_ino)
            if st.st_nlink > 1 and key in seen_links:
                # hardlink: same ino, zero data length
                out += _entry(arcname, stat.S_IFREG | perm, b"", seen_links[key], 1, mtime, size=0)
                continue
            if st.st_nlink > 1:
                seen_links[key] = ino
            with open(full, "rb") as f:
                data = f.read()
            out += _entry(arcname, stat.S_IFREG | perm, data, ino, st.st_nlink, mtime)
        elif stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode):
            mode = (stat.S_IFCHR if stat.S_ISCHR(st.st_mode) else stat.S_IFBLK) | perm
            out += _entry(arcname, mode, b"", ino, 1, mtime, rdev=st.st_rdev)
        elif stat.S_ISFIFO(st.st_mode):
            out += _entry(arcname, stat.S_IFIFO | perm, b"", ino, 1, mtime)
        else:
            sys.stderr.write(f"mkcpio: skipping unsupported file type: {rel}\n")

    out += _entry(TRAILER, 0, b"", 0, 1, mtime)
    # newc archives are padded to a 512-byte boundary
    out += b"\0" * _pad4(len(out) % 512 or 512)
    if len(out) % 512:
        out += b"\0" * (512 - len(out) % 512)
    return bytes(out)


def parse(blob):
    """Yield (name, mode, ino, nlink, data) tuples, including the trailer."""
    pos = 0
    while True:
        if pos + HEADER_LEN > len(blob):
            raise CpioError(f"truncated archive at offset {pos}")
        fields = struct.unpack_from(HEADER_FMT, blob, pos)
        if fields[0] != MAGIC:
            raise CpioError(f"bad cpio magic {fields[0]!r} at offset {pos}")
        ino = int(fields[1], 16)
        mode = int(fields[2], 16)
        nlink = int(fields[5], 16)
        size = int(fields[7], 16)
        namesize = int(fields[12], 16)
        name_start = pos + HEADER_LEN
        name_b = blob[name_start:name_start + namesize]
        if not name_b.endswith(b"\0"):
            raise CpioError(f"unterminated name at offset {pos}")
        name = name_b[:-1].decode(errors="replace")
        data_start = name_start + namesize + _pad4(HEADER_LEN + namesize)
        data = blob[data_start:data_start + size]
        if len(data) != size:
            raise CpioError(f"truncated data for {name}: {len(data)} of {size}")
        yield name, mode, ino, nlink, data
        if name == TRAILER:
            return
        pos = data_start + size + _pad4(size)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("root")
    c.add_argument("-o", "--out", required=True)
    c.add_argument("--mtime", type=int,
                   default=int(os.environ.get("SOURCE_DATE_EPOCH", "0")))
    l = sub.add_parser("list")
    l.add_argument("archive")
    v = sub.add_parser("verify")
    v.add_argument("archive")
    args = p.parse_args(argv)

    try:
        if args.cmd == "create":
            blob = create(args.root, args.mtime)
            with open(args.out, "wb") as f:
                f.write(blob)
            count = sum(1 for _ in parse(blob)) - 1
            print(f"[mkcpio] {args.out}: {count} entries, {len(blob)} bytes")
            return 0
        with open(args.archive, "rb") as f:
            blob = f.read()
        if args.cmd == "list":
            for name, mode, ino, nlink, data in parse(blob):
                kind = "d" if stat.S_ISDIR(mode) else "l" if stat.S_ISLNK(mode) else \
                       "c" if stat.S_ISCHR(mode) else "b" if stat.S_ISBLK(mode) else "-"
                extra = ""
                if stat.S_ISLNK(mode):
                    extra = " -> " + data.decode(errors="replace")
                print(f"{kind} {oct(mode & 0o7777)[2:]:>4} {len(data):>8} {name}{extra}")
            return 0
        n = 0
        for _ in parse(blob):
            n += 1
        print(f"[mkcpio] {args.archive}: OK, {n - 1} entries")
        return 0
    except (OSError, CpioError) as e:
        sys.stderr.write(f"mkcpio: error: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
