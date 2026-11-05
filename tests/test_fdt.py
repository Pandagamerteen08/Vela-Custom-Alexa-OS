#!/usr/bin/env python3
"""Tests for tools/fdt.py and tools/patch-dtb.py.

A real dtc is not required: the round-trip test builds a tree with the same
writer that the patcher uses, then re-parses it, which catches serialisation
errors but not shared misconceptions. So there is also an optional test against
a real dtc-produced blob (tests/data/*.dtb or $FDT_REAL_DTB) — run it before
trusting the patcher on the device's DTB.
"""

import os
import struct
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import fdt  # noqa: E402
from fdt import DeviceTree, FdtError  # noqa: E402


def sample_tree():
    dt = DeviceTree()
    root = dt.root
    root.set("#address-cells", struct.pack(">I", 2))
    root.set("#size-cells", struct.pack(">I", 2))
    root.set("model", "Amazon Echo Show 5 (2nd gen)")
    chosen = fdt.Node("chosen", parent=root)
    chosen.set("bootargs", "console=ttyMT0,921600n1")
    root.children.append(chosen)
    soc = fdt.Node("soc", parent=root)
    root.children.append(soc)
    sound = fdt.Node("sound", parent=soc)
    sound.set("compatible", "mediatek,mt8163-audio\0")
    sound.set("amzn,mic-downmix", b"")
    sound.set("reg", struct.pack(">I", 0x11220000))
    soc.children.append(sound)
    return dt


class FdtRoundTrip(unittest.TestCase):
    def test_header_fields(self):
        blob = sample_tree().to_bytes()
        magic, total, off_struct, off_strings, off_rsv, version, lastcomp, cpuid, \
            size_strings, size_struct = struct.unpack_from(">10I", blob, 0)
        self.assertEqual(magic, fdt.FDT_MAGIC)
        self.assertEqual(total, len(blob))
        self.assertEqual(version, 17)
        self.assertEqual(lastcomp, 16)
        self.assertLess(off_rsv, off_struct)
        self.assertLess(off_struct, off_strings)
        self.assertEqual(off_strings + size_strings, total)

    def test_reextract_identical_tree(self):
        src = sample_tree()
        parsed = DeviceTree.parse(src.to_bytes())
        # `model` is a property of the root node, not a child node
        self.assertEqual(parsed.root.get_str("model"),
                         "Amazon Echo Show 5 (2nd gen)")
        sound = parsed.find("/soc/sound")
        self.assertIsNotNone(sound)
        self.assertEqual(sound.get("amzn,mic-downmix"), b"")
        self.assertEqual(struct.unpack(">I", sound.get("reg"))[0], 0x11220000)
        # node order and property order survive
        self.assertEqual([c.name for c in parsed.root.children], ["chosen", "soc"])
        self.assertEqual([n for n, _ in sound.props],
                         ["compatible", "amzn,mic-downmix", "reg"])

    def test_serialise_is_stable(self):
        a = DeviceTree.parse(sample_tree().to_bytes())
        b = DeviceTree.parse(a.to_bytes())
        self.assertEqual(a.to_bytes(), b.to_bytes())

    def test_delete_mic_downmix(self):
        dt = DeviceTree.parse(sample_tree().to_bytes())
        hits = dt.delete_prop_everywhere("amzn,mic-downmix")
        self.assertEqual(hits, ["/soc/sound"])
        again = DeviceTree.parse(dt.to_bytes())
        self.assertIsNone(again.find("/soc/sound").get("amzn,mic-downmix"))
        self.assertEqual(dt.delete_prop_everywhere("amzn,mic-downmix"), [])

    def test_bootargs_update(self):
        dt = DeviceTree.parse(sample_tree().to_bytes())
        dt.find("/chosen").set("bootargs", "console=ttyMT0,921600n1 firmware_class.path=/lib/firmware")
        reparsed = DeviceTree.parse(dt.to_bytes())
        self.assertIn("firmware_class.path", reparsed.bootargs())

    def test_find_by_basename_and_full_name(self):
        dt = DeviceTree.parse(sample_tree().to_bytes())
        self.assertIsNotNone(dt.find("/soc/sound"))
        self.assertIsNotNone(dt.find("soc"))
        self.assertIsNone(dt.find("/nope"))

    def test_rejects_garbage(self):
        with self.assertRaises(FdtError):
            DeviceTree.parse(b"not a device tree at all")
        with self.assertRaises(FdtError):
            DeviceTree.parse(b"\xd0\x0d\xfe\xed" + b"\0" * 8)

    def test_truncated_struct_block(self):
        blob = bytearray(sample_tree().to_bytes())
        with self.assertRaises(FdtError):
            DeviceTree.parse(bytes(blob[:len(blob) // 2]))


class SetPropTyped(unittest.TestCase):
    def test_types(self):
        node = fdt.Node("n")
        fdt.set_prop_typed(node, "s", "str:hello")
        self.assertEqual(node.get("s"), b"hello\0")
        fdt.set_prop_typed(node, "u", "u32:1,0x20")
        self.assertEqual(struct.unpack(">2I", node.get("u")), (1, 0x20))
        fdt.set_prop_typed(node, "b", "bytes:deadbeef")
        self.assertEqual(node.get("b"), b"\xde\xad\xbe\xef")
        with self.assertRaises(FdtError):
            fdt.set_prop_typed(node, "x", "nonsense:1")


class Cli(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, os.path.join(ROOT, "tools", "patch-dtb.py"), *args],
                              capture_output=True, text=True)

    def test_patch_and_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.dtb")
            out = os.path.join(tmp, "out.dtb")
            with open(src, "wb") as f:
                f.write(sample_tree().to_bytes())
            r = self.run_cli(src, "-o", out, "--no-mic-downmix")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("removed amzn,mic-downmix", r.stdout)
            with open(out, "rb") as f:
                patched = DeviceTree.parse(f.read())
            self.assertIsNone(patched.find("/soc/sound").get("amzn,mic-downmix"))

            r = self.run_cli(out, "--list")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("sound", r.stdout)
            self.assertIn("bootargs", r.stdout)

    def test_missing_property_warns_but_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.dtb")
            with open(src, "wb") as f:
                f.write(sample_tree().to_bytes())
            r = self.run_cli(src, "-o", os.path.join(tmp, "o.dtb"), "--delete-prop", "no,such,prop")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("warning", r.stderr)

    def test_bad_set_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.dtb")
            with open(src, "wb") as f:
                f.write(sample_tree().to_bytes())
            r = self.run_cli(src, "-o", os.path.join(tmp, "o.dtb"),
                             "--set", "/nope:x:u32:1")
            self.assertEqual(r.returncode, 1)
            self.assertIn("no node", r.stderr)

    def test_appended_dtb_scan(self):
        """A DTB at the end of a kernel Image must be found."""
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "Image")
            with open(src, "wb") as f:
                f.write(b"\x00" * 4096 + sample_tree().to_bytes())
            r = self.run_cli(src, "--print-offset", "--show-bootargs")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("DTB at byte offset 4096", r.stdout)


class RealDtb(unittest.TestCase):
    """Optional: parse a genuine dtc-produced blob if one is available."""

    def find_real_dtb(self):
        for cand in (os.environ.get("FDT_REAL_DTB"),
                     os.path.join(HERE, "data", "real.dtb"),
                     "/tmp/tc/real.dtb"):
            if cand and os.path.exists(cand):
                return cand
        return None

    def test_real_dtb_roundtrip(self):
        path = self.find_real_dtb()
        if not path:
            self.skipTest("no real DTB available (set FDT_REAL_DTB)")
        with open(path, "rb") as f:
            blob = f.read()
        dt = DeviceTree.parse(blob)
        flat = [(n.full_path(), [(k, v) for k, v in n.props]) for n in dt.root.walk()]
        again = DeviceTree.parse(dt.to_bytes())
        flat2 = [(n.full_path(), [(k, v) for k, v in n.props]) for n in again.root.walk()]
        self.assertEqual(flat, flat2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
