#!/usr/bin/env python3
"""Android boot image v0 packer/unpacker for cronos (Echo Show 5 2nd gen, MT8163).

Why this exists instead of AOSP's mkbootimg:
  * amonet's LK on cronos wants header v0 with page_size 2048 and fixed load
    addresses (kernel 0x40080000, ramdisk 0x69244e00, tags 0x48000000).
  * the boot partition is 16 MB and we want a hard failure before flashing if we
    overshoot it.
  * zero dependencies, so it runs on any host with python3.

Format reference: system/tools/mkbootimg/include/bootimg/bootimg.h (boot_img_hdr_v0).
Every field below is little-endian.

    off  size  field
    0x000   8  magic "ANDROID!"
    0x008   4  kernel_size
    0x00c   4  kernel_addr
    0x010   4  ramdisk_size
    0x014   4  ramdisk_addr
    0x018   4  second_size
    0x01c   4  second_addr
    0x020   4  tags_addr
    0x024   4  page_size
    0x028   4  header_version   (0 for us)
    0x02c   4  os_version
    0x030  16  name
    0x040 512  cmdline
    0x240   8  id (sha1 over payloads+lengths, truncated)
    0x248 1024 extra_cmdline
    total 1608, padded up to page_size

Usage:
    ./mkbootimg.py unpack stock-boot.img --outdir /tmp/stock     # prints cmdline
    ./mkbootimg.py pack   --kernel Image.gz --ramdisk initramfs.cpio.gz \
                          --cmdline-file kernel/cmdline.txt --out boot.img
    ./mkbootimg.py info   boot.img
"""

import argparse
import hashlib
import os
import struct
import sys

MAGIC = b"ANDROID!"
HEADER_V0_SIZE = 1608
CMDLINE_MAX = 512
EXTRA_CMDLINE_MAX = 1024
NAME_MAX = 16

# cronos ground truth (from the stock boot image header).
PAGE_SIZE = 2048
KERNEL_ADDR = 0x40080000
RAMDISK_ADDR = 0x69244E00
SECOND_ADDR = 0x00F00000
TAGS_ADDR = 0x48000000
BOOT_PARTITION_BYTES = 16 * 1024 * 1024

U32 = struct.Struct("<I")


class BootImageError(Exception):
    pass


def _pad(data, page):
    rem = len(data) % page
    if rem == 0:
        return data
    return data + b"\0" * (page - rem)


def _blob_id(kernel, ramdisk, second, recovery_dtbo=None, dtb=None, header_version=0):
    """sha1 over each payload immediately followed by its u32 length."""
    h = hashlib.sha1()
    for payload in (kernel, ramdisk, second):
        h.update(payload)
        h.update(U32.pack(len(payload)))
    if header_version >= 1:
        for payload in (recovery_dtbo or b"", dtb or b""):
            h.update(payload)
            h.update(U32.pack(len(payload)))
    return h.digest()[:8]


def build_header(kernel_size, ramdisk_size, second_size,
                 kernel_addr=KERNEL_ADDR, ramdisk_addr=RAMDISK_ADDR,
                 second_addr=SECOND_ADDR, tags_addr=TAGS_ADDR,
                 page_size=PAGE_SIZE, name="", cmdline="", extra_cmdline="",
                 os_version=0, blob_id=b"\0" * 8):
    name_b = name.encode()
    cmdline_b = cmdline.encode()
    extra_b = extra_cmdline.encode()
    if len(name_b) >= NAME_MAX:
        raise BootImageError(f"name too long ({len(name_b)} >= {NAME_MAX})")
    if len(cmdline_b) >= CMDLINE_MAX:
        raise BootImageError(f"cmdline too long ({len(cmdline_b)} >= {CMDLINE_MAX}); "
                             "use --extra-cmdline (1024 bytes) or trim it")
    if len(extra_b) >= EXTRA_CMDLINE_MAX:
        raise BootImageError(f"extra cmdline too long ({len(extra_b)} >= {EXTRA_CMDLINE_MAX})")
    if len(blob_id) != 8:
        raise BootImageError("blob id must be 8 bytes")
    hdr = bytearray()
    hdr += MAGIC
    hdr += U32.pack(kernel_size)
    hdr += U32.pack(kernel_addr)
    hdr += U32.pack(ramdisk_size)
    hdr += U32.pack(ramdisk_addr)
    hdr += U32.pack(second_size)
    hdr += U32.pack(second_addr)
    hdr += U32.pack(tags_addr)
    hdr += U32.pack(page_size)
    hdr += U32.pack(0)          # header_version: v0 only
    hdr += U32.pack(os_version)
    hdr += name_b.ljust(NAME_MAX, b"\0")
    hdr += cmdline_b.ljust(CMDLINE_MAX, b"\0")
    hdr += blob_id
    hdr += extra_b.ljust(EXTRA_CMDLINE_MAX, b"\0")
    if len(hdr) != HEADER_V0_SIZE:
        raise BootImageError(f"internal: header is {len(hdr)} bytes, expected {HEADER_V0_SIZE}")
    return bytes(hdr)


def pack(kernel, ramdisk, second=b"", **kw):
    page = kw.get("page_size", PAGE_SIZE)
    blob_id = _blob_id(kernel, ramdisk, second)
    hdr = build_header(len(kernel), len(ramdisk), len(second), blob_id=blob_id, **kw)
    out = _pad(hdr, page) + _pad(kernel, page) + _pad(ramdisk, page)
    if second:
        out += _pad(second, page)
    return out


def unpack(blob):
    if len(blob) < 40 or blob[:8] != MAGIC:
        raise BootImageError("not an Android boot image (bad magic)")
    (kernel_size, kernel_addr, ramdisk_size, ramdisk_addr, second_size,
     second_addr, tags_addr, page_size, header_version, os_version) = struct.unpack_from("<10I", blob, 8)

    if header_version >= 3:
        raise BootImageError(
            f"header v{header_version} is not supported (cronos LK needs v0). "
            "Re-run mkbootimg on the stock image with 'header_version 0', or use "
            "AOSP unpack_bootimg for inspection only.")
    if page_size not in (2048, 4096, 8192, 16384, 65536) or (page_size & (page_size - 1)):
        raise BootImageError(f"implausible page_size {page_size} (header probably not v0)")

    name = blob[0x30:0x40].split(b"\0", 1)[0].decode(errors="replace")
    cmdline = blob[0x40:0x240].split(b"\0", 1)[0].decode(errors="replace")
    blob_id = blob[0x240:0x248]
    extra_cmdline = blob[0x248:0x648].split(b"\0", 1)[0].decode(errors="replace")

    pos = page_size
    kernel = blob[pos:pos + kernel_size]; pos += _round(kernel_size, page_size)
    ramdisk = blob[pos:pos + ramdisk_size]; pos += _round(ramdisk_size, page_size)
    second = blob[pos:pos + second_size]; pos += _round(second_size, page_size)
    rec_dtbo = dtb = b""
    if header_version >= 1:
        rec_size, rec_off, dtb_size = struct.unpack_from("<3I", blob, 0x648)
        pos = _round(pos, page_size)
        rec_dtbo = blob[pos:pos + rec_size]; pos += _round(rec_size, page_size)
        dtb = blob[pos:pos + dtb_size]
    for label, got, want in (("kernel", len(kernel), kernel_size),
                             ("ramdisk", len(ramdisk), ramdisk_size)):
        if got != want:
            raise BootImageError(f"truncated image: {label} {got} of {want} bytes")

    if _blob_id(kernel, ramdisk, second, rec_dtbo, dtb, header_version) != blob_id:
        sys.stderr.write("warning: id field does not match payload hashes "
                         "(stock images are sometimes built this way; not fatal)\n")

    return {
        "page_size": page_size, "header_version": header_version,
        "kernel_size": kernel_size, "kernel_addr": kernel_addr,
        "ramdisk_size": ramdisk_size, "ramdisk_addr": ramdisk_addr,
        "second_size": second_size, "second_addr": second_addr,
        "tags_addr": tags_addr, "os_version": os_version,
        "name": name, "cmdline": cmdline, "extra_cmdline": extra_cmdline,
        "kernel": kernel, "ramdisk": ramdisk, "second": second,
        "total_size": len(blob),
    }


def _round(n, page):
    return (n + page - 1) // page * page


def cmd_pack(args):
    kernel = _read(args.kernel)
    ramdisk = _read(args.ramdisk) if args.ramdisk else b""
    second = _read(args.second) if args.second else b""

    if args.cmdline_file:
        cmdline = _read(args.cmdline_file).decode().replace("\n", " ").strip()
    else:
        cmdline = args.cmdline or ""

    img = pack(kernel, ramdisk, second,
               page_size=args.page_size,
               kernel_addr=args.kernel_addr,
               ramdisk_addr=args.ramdisk_addr,
               tags_addr=args.tags_addr,
               name=args.name,
               cmdline=cmdline,
               extra_cmdline=args.extra_cmdline or "")

    if len(img) > BOOT_PARTITION_BYTES:
        raise BootImageError(
            f"boot.img is {len(img)} bytes but the partition (p9) is "
            f"{BOOT_PARTITION_BYTES} bytes. LK will reject or truncate it. "
            "Trim the kernel config, drop dropbear from the initramfs, "
            "or switch the initramfs from gzip to xz.")

    _write(args.out, img)
    print(f"[mkbootimg] wrote {args.out}: {len(img)} bytes "
          f"({len(img) * 100 // BOOT_PARTITION_BYTES}% of p9)")
    print(f"[mkbootimg]   kernel   {len(kernel):>9} bytes @ 0x{args.kernel_addr:08x}")
    print(f"[mkbootimg]   ramdisk  {len(ramdisk):>9} bytes @ 0x{args.ramdisk_addr:08x}")
    _warn_payload(kernel, "kernel")
    _warn_payload(ramdisk, "ramdisk")
    print(f"[mkbootimg]   cmdline  {cmdline!r}")
    return 0


def _warn_payload(data, label):
    """LK on MT8163 accepts gzip'd payloads; a raw one also works. Say which it is."""
    if not data:
        return
    if data[:2] == b"\x1f\x8b":
        print(f"[mkbootimg]   {label} is gzip")
    elif data[:4] == b"\xfd7zX" or data[:6] == b"\xfd7zXZ\x00":
        print(f"[mkbootimg]   {label} is xz  (kernel must have CONFIG_RD_XZ=y)")
    elif data[:3] == b"BZh":
        print(f"[mkbootimg]   {label} is bzip2")
    elif data[:4] == b"\x02!\x4c\x18":
        print(f"[mkbootimg]   {label} is lz4  (kernel must have CONFIG_RD_LZ4=y)")
    elif label == "kernel":
        print("[mkbootimg]   kernel is RAW (not gzip). LK accepts this, but it is "
              "7-9 MB instead of 5-7 MB; check the 16 MB budget.")
    else:
        print(f"[mkbootimg]   {label} is RAW cpio (uncompressed) — largest option")


def cmd_unpack(args):
    info = unpack(_read(args.image))
    outdir = args.outdir
    if outdir:
        os.makedirs(outdir, exist_ok=True)
        _write(os.path.join(outdir, "kernel"), info["kernel"])
        _write(os.path.join(outdir, "ramdisk"), info["ramdisk"])
        if info["second"]:
            _write(os.path.join(outdir, "second"), info["second"])
        _write(os.path.join(outdir, "cmdline.txt"),
               (info["cmdline"] + " " + info["extra_cmdline"]).strip().encode() + b"\n")
        _write(os.path.join(outdir, "header.txt"), _describe(info).encode())
    sys.stdout.write(_describe(info))
    print(f"\n[mkbootimg] total {info['total_size']} bytes "
          f"({info['total_size'] * 100 // BOOT_PARTITION_BYTES}% of p9)")
    if outdir:
        print(f"[mkbootimg] extracted to {outdir}/")
    return 0


def cmd_info(args):
    info = unpack(_read(args.image))
    sys.stdout.write(_describe(info))
    return 0


def _describe(info):
    lines = [
        f"magic           ANDROID!",
        f"header_version  {info['header_version']}",
        f"page_size       {info['page_size']}",
        f"kernel          {info['kernel_size']:>9} bytes @ 0x{info['kernel_addr']:08x}",
        f"ramdisk         {info['ramdisk_size']:>9} bytes @ 0x{info['ramdisk_addr']:08x}",
        f"second          {info['second_size']:>9} bytes @ 0x{info['second_addr']:08x}",
        f"tags_addr       0x{info['tags_addr']:08x}",
        f"name            {info['name']!r}",
        f"cmdline         {info['cmdline']!r}",
        f"extra_cmdline   {info['extra_cmdline']!r}",
    ]
    expected = (info["page_size"], 2048, 0x40080000, 0x69244e00, 0x48000000)
    got = (info["page_size"], 2048, info["kernel_addr"], info["ramdisk_addr"], info["tags_addr"])
    if got != expected:
        lines.append("WARNING: header differs from cronos ground truth "
                     "(page 2048, kernel 0x40080000, ramdisk 0x69244e00, tags 0x48000000). "
                     "Do not flash this unless you know why.")
    return "\n".join(lines) + "\n"


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _write(path, data):
    with open(path, "wb") as f:
        f.write(data)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pk = sub.add_parser("pack", help="build a boot.img")
    pk.add_argument("--kernel", required=True)
    pk.add_argument("--ramdisk")
    pk.add_argument("--second")
    pk.add_argument("--cmdline", default="")
    pk.add_argument("--cmdline-file")
    pk.add_argument("--extra-cmdline", default="")
    pk.add_argument("--out", required=True)
    pk.add_argument("--name", default="cronos-min")
    pk.add_argument("--page-size", type=lambda s: int(s, 0), default=PAGE_SIZE)
    pk.add_argument("--kernel-addr", type=lambda s: int(s, 0), default=KERNEL_ADDR)
    pk.add_argument("--ramdisk-addr", type=lambda s: int(s, 0), default=RAMDISK_ADDR)
    pk.add_argument("--tags-addr", type=lambda s: int(s, 0), default=TAGS_ADDR)
    pk.set_defaults(func=cmd_pack)

    up = sub.add_parser("unpack", help="extract payloads and print the header")
    up.add_argument("image")
    up.add_argument("--outdir")
    up.set_defaults(func=cmd_unpack)

    inf = sub.add_parser("info", help="print the header only")
    inf.add_argument("image")
    inf.set_defaults(func=cmd_info)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except BootImageError as e:
        sys.stderr.write(f"mkbootimg: error: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
