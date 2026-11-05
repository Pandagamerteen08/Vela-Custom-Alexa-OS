#!/usr/bin/env python3
"""Tests for tools/mkcpio.py and tools/kconfig.py."""

import os
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import kconfig  # noqa: E402
import mkcpio  # noqa: E402


class Cpio(unittest.TestCase):
    def build_tree(self, tmp):
        os.makedirs(os.path.join(tmp, "bin"))
        os.makedirs(os.path.join(tmp, "sbin"))
        with open(os.path.join(tmp, "init"), "w") as f:
            f.write("#!/bin/sh\nexec /bin/sh\n")
        os.chmod(os.path.join(tmp, "init"), 0o755)
        with open(os.path.join(tmp, "bin", "busybox"), "wb") as f:
            f.write(b"\x7fELF" + b"\0" * 200)
        os.symlink("busybox", os.path.join(tmp, "bin", "sh"))
        os.symlink("/bin/busybox", os.path.join(tmp, "sbin", "init"))
        os.link(os.path.join(tmp, "bin", "busybox"), os.path.join(tmp, "bin", "cat"))

    def test_create_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.build_tree(os.path.join(tmp, "root"))
            blob = mkcpio.create(os.path.join(tmp, "root"))
            entries = {n: (m, d) for n, m, _, _, d in mkcpio.parse(blob)}
            self.assertIn("./init", entries)
            self.assertTrue(stat.S_ISREG(entries["./init"][0]))
            self.assertEqual(entries["./init"][0] & 0o777, 0o755)
            self.assertEqual(entries["./init"][1], b"#!/bin/sh\nexec /bin/sh\n")
            self.assertTrue(stat.S_ISLNK(entries["./bin/sh"][0]))
            self.assertEqual(entries["./bin/sh"][1], b"busybox")
            self.assertTrue(stat.S_ISDIR(entries["./bin"][0]))
            self.assertNotIn("", entries)

    def test_hardlink_shares_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.build_tree(os.path.join(tmp, "root"))
            blob = mkcpio.create(os.path.join(tmp, "root"))
            recs = list(mkcpio.parse(blob))
            by_name = {n: (ino, link, data) for n, m, ino, link, data in recs if stat.S_ISREG(m)}
            self.assertEqual(by_name["./bin/cat"][0], by_name["./bin/busybox"][0])
            self.assertEqual(by_name["./bin/cat"][2], b"")

    def test_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "root")
            self.build_tree(root)
            a = mkcpio.create(root)
            b = mkcpio.create(root)
            self.assertEqual(a, b)

    def test_512_alignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "root")
            self.build_tree(root)
            self.assertEqual(len(mkcpio.create(root)) % 512, 0)

    def test_bad_magic(self):
        with self.assertRaises(mkcpio.CpioError):
            list(mkcpio.parse(b"070702" + b"0" * 200))


class Kconfig(unittest.TestCase):
    BASE = (
        "#\n# Automatically generated file; DO NOT EDIT.\n#\n"
        "CONFIG_ARM64=y\n"
        "# CONFIG_DRM is not set\n"
        "CONFIG_SQUASHFS=y\n"
        'CONFIG_CMDLINE="console=ttyMT0"\n'
        "CONFIG_DEBUG_INFO=y\n"
    )

    def test_apply_in_place_and_disable(self):
        frag = "CONFIG_SQUASHFS_XZ=y\n-CONFIG_DEBUG_INFO\nCONFIG_SQUASHFS=y\n"
        out, changes = kconfig.apply(self.BASE, [(frag, "test.fragment")])
        self.assertIn("CONFIG_SQUASHFS_XZ=y", out)
        self.assertIn("# CONFIG_DEBUG_INFO is not set", out)
        self.assertNotIn("CONFIG_DEBUG_INFO=y", out)
        self.assertEqual(len(changes), 2)          # SQUASHFS was already =y
        # order preserved: SQUASHFS_XZ appended, DEBUG_INFO kept its slot
        lines = out.splitlines()
        self.assertLess(lines.index("CONFIG_SQUASHFS=y"), lines.index("# CONFIG_DEBUG_INFO is not set"))

    def test_apply_is_idempotent(self):
        frag = "CONFIG_SQUASHFS_XZ=y\n-CONFIG_DEBUG_INFO\n"
        once, _ = kconfig.apply(self.BASE, [(frag, "f")])
        twice, changes = kconfig.apply(once, [(frag, "f")])
        self.assertEqual(once, twice)
        self.assertEqual(changes, [])

    def test_check_pass_and_fail(self):
        self.assertEqual(kconfig.check(self.BASE, "CONFIG_ARM64=y\n-CONFIG_DRM\n"), [])
        problems = kconfig.check(self.BASE, "CONFIG_SQUASHFS_XZ=y\nCONFIG_ARM64=m\n")
        self.assertEqual(len(problems), 2)

    def test_comments_and_bad_syntax(self):
        self.assertEqual(kconfig.parse_fragment("# comment\n\nCONFIG_A=y  # trailing\n"),
                         [("CONFIG_A", "y", 3)])
        with self.assertRaises(kconfig.KconfigError):
            kconfig.parse_fragment("SQUASHFS=y\n")

    def test_string_value_kept(self):
        out, _ = kconfig.apply(self.BASE, [('CONFIG_CMDLINE="console=ttyMT0,921600n1"\n', "f")])
        self.assertIn('CONFIG_CMDLINE="console=ttyMT0,921600n1"', out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
