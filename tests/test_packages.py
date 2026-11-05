#!/usr/bin/env python3
"""Tests for tools/packages.py — the 'every package has a justification' contract."""

import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import packages  # noqa: E402


class Parse(unittest.TestCase):
    def test_minimal_valid(self):
        pkgs, problems = packages.parse(
            "# header\nbusybox   # PID 1 and the applet farm\n")
        self.assertEqual(problems, [])
        self.assertEqual(pkgs[0].name, "busybox")
        self.assertEqual(pkgs[0].spec, "busybox")
        self.assertIn("PID 1", pkgs[0].why)

    def test_pin(self):
        pkgs, problems = packages.parse("wpa_supplicant=2.9-r6   # replay counters\n")
        self.assertEqual(problems, [])
        self.assertEqual(pkgs[0].spec, "wpa_supplicant=2.9-r6")

    def test_missing_justification_fails(self):
        _, problems = packages.parse("musl\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("no justification", problems[0])

    def test_forbidden_package_fails(self):
        _, problems = packages.parse("python3   # convenience\n")
        self.assertTrue(any("forbidden" in p for p in problems))

    def test_bad_pin(self):
        _, problems = packages.parse("foo=latest   # nope\n")
        self.assertTrue(any("should start with a digit" in p for p in problems))

    def test_duplicate(self):
        _, problems = packages.parse("musl  # a\nmusl  # b\n")
        self.assertTrue(any("duplicate" in p for p in problems))

    def test_garbage_line(self):
        _, problems = packages.parse("this is not a package line\n")
        self.assertTrue(problems)


class RealList(unittest.TestCase):
    def test_project_list_is_valid(self):
        """The actual rootfs/packages.txt must satisfy the project's own rule."""
        path = os.path.join(ROOT, "rootfs", "packages.txt")
        with open(path) as f:
            pkgs, problems = packages.parse(f.read())
        self.assertEqual(problems, [], f"packages.txt: {problems}")
        self.assertGreaterEqual(len(pkgs), 5)
        names = [p.name for p in pkgs]
        for banned in ("openssl", "python3", "bluez", "udev"):
            self.assertNotIn(banned, names)

    def test_cli(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "packages.py"),
                            "check", "--file", os.path.join(ROOT, "rootfs", "packages.txt")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("all justified", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
