#!/usr/bin/env python3
"""Tests for tools/mkbootimg.py — header layout, round-trip, and the p9 size gate."""

import os
import struct
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import mkbootimg  # noqa: E402
from mkbootimg import BootImageError  # noqa: E402


def gz(data):
    return b"\x1f\x8b\x08\x00" + data


class HeaderLayout(unittest.TestCase):
    def test_offsets_match_aosp(self):
        hdr = mkbootimg.build_header(1234, 5678, 0, cmdline="console=ttyMT0", name="cronos-min")
        self.assertEqual(len(hdr), 1608)
        self.assertEqual(hdr[:8], b"ANDROID!")
        self.assertEqual(struct.unpack_from("<I", hdr, 0x008)[0], 1234)   # kernel_size
        self.assertEqual(struct.unpack_from("<I", hdr, 0x00c)[0], 0x40080000)
        self.assertEqual(struct.unpack_from("<I", hdr, 0x010)[0], 5678)   # ramdisk_size
        self.assertEqual(struct.unpack_from("<I", hdr, 0x014)[0], 0x69244E00)
        self.assertEqual(struct.unpack_from("<I", hdr, 0x020)[0], 0x48000000)
        self.assertEqual(struct.unpack_from("<I", hdr, 0x024)[0], 2048)
        self.assertEqual(struct.unpack_from("<I", hdr, 0x028)[0], 0)      # header v0
        self.assertEqual(hdr[0x30:0x30 + 10], b"cronos-min")
        cmd = hdr[0x40:0x240].split(b"\0")[0]
        self.assertEqual(cmd, b"console=ttyMT0")

    def test_cmdline_limits(self):
        with self.assertRaises(BootImageError):
            mkbootimg.build_header(1, 1, 0, cmdline="x" * 512)
        with self.assertRaises(BootImageError):
            mkbootimg.build_header(1, 1, 0, extra_cmdline="x" * 1024)
        with self.assertRaises(BootImageError):
            mkbootimg.build_header(1, 1, 0, name="x" * 16)


class RoundTrip(unittest.TestCase):
    def test_small_image(self):
        kernel = gz(b"K" * 3000)
        ramdisk = gz(b"R" * 5000)
        img = mkbootimg.pack(kernel, ramdisk, cmdline="console=ttyMT0,921600n1")
        # header page + whole pages for each payload, nothing more
        def pages(n):
            return (n + 2047) // 2048 * 2048
        self.assertEqual(len(img), 2048 + pages(len(kernel)) + pages(len(ramdisk)))
        info = mkbootimg.unpack(img)
        self.assertEqual(info["kernel"], kernel)
        self.assertEqual(info["ramdisk"], ramdisk)
        self.assertEqual(info["kernel_size"], len(kernel))
        self.assertEqual(info["ramdisk_size"], len(ramdisk))
        self.assertEqual(info["page_size"], 2048)
        self.assertEqual(info["kernel_addr"], 0x40080000)
        self.assertEqual(info["ramdisk_addr"], 0x69244E00)
        self.assertEqual(info["tags_addr"], 0x48000000)
        self.assertEqual(info["cmdline"], "console=ttyMT0,921600n1")

    def test_multi_page_payload(self):
        kernel = gz(b"K" * 9000)
        ramdisk = gz(b"R" * 100)
        img = mkbootimg.pack(kernel, ramdisk)
        info = mkbootimg.unpack(img)
        self.assertEqual(info["kernel"], kernel)
        self.assertEqual(info["ramdisk"], ramdisk)
        self.assertEqual(len(img) % 2048, 0)

    def test_id_field(self):
        kernel, ramdisk = b"abc", b"def"
        img = mkbootimg.pack(kernel, ramdisk)
        expected = mkbootimg._blob_id(kernel, ramdisk, b"")
        self.assertEqual(img[0x240:0x248], expected)

    def test_rejects_non_boot_image(self):
        with self.assertRaises(BootImageError):
            mkbootimg.unpack(b"x" * 4096)

    def test_rejects_v3_header(self):
        hdr = bytearray(mkbootimg.build_header(1, 1, 0))
        struct.pack_into("<I", hdr, 0x028, 3)
        with self.assertRaises(BootImageError) as ctx:
            mkbootimg.unpack(bytes(hdr) + b"\0" * 4096)
        self.assertIn("v0", str(ctx.exception))

    def test_partition_gate(self):
        """The 16 MB partition limit must stop the build, not LK."""
        with tempfile.TemporaryDirectory() as tmp:
            k = os.path.join(tmp, "Image.gz")
            with open(k, "wb") as f:
                f.write(b"\x1f\x8b" + b"K" * (17 * 1024 * 1024))
            out = os.path.join(tmp, "boot.img")
            rc = mkbootimg.main(["pack", "--kernel", k, "--out", out])
            self.assertEqual(rc, 1)
            self.assertFalse(os.path.exists(out))


class Cli(unittest.TestCase):
    def test_pack_unpack_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = os.path.join(tmp, "Image.gz")
            r = os.path.join(tmp, "initramfs.cpio.gz")
            c = os.path.join(tmp, "cmdline.txt")
            img = os.path.join(tmp, "boot.img")
            with open(k, "wb") as f:
                f.write(gz(b"kernel" * 500))
            with open(r, "wb") as f:
                f.write(gz(b"ramdisk" * 500))
            with open(c, "w") as f:
                f.write("console=ttyMT0,921600n1\nfirmware_class.path=/lib/firmware\n")
            rc = mkbootimg.main(["pack", "--kernel", k, "--ramdisk", r,
                                 "--cmdline-file", c, "--out", img])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(img))

            outdir = os.path.join(tmp, "unpacked")
            rc = mkbootimg.main(["unpack", img, "--outdir", outdir])
            self.assertEqual(rc, 0)
            with open(os.path.join(outdir, "cmdline.txt")) as f:
                cmd = f.read()
            self.assertIn("firmware_class.path=/lib/firmware", cmd)
            self.assertNotIn("\n", cmd.strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
