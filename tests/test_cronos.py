"""Tests for tools/cronos.py - the cross-platform entry point.

These matter more than they look: everything here must pass on Linux, macOS and
Windows, which is the only thing that makes the portability claim checkable in
CI rather than aspirational.
"""

import hashlib
import io
import os
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import cronos  # noqa: E402


class TestHelpers(unittest.TestCase):
    def test_sha256_matches_hashlib(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b"cronos" * 1000)
            name = fh.name
        try:
            with open(name, "rb") as fh:
                want = hashlib.sha256(fh.read()).hexdigest()
            self.assertEqual(cronos.sha256(name), want)
        finally:
            os.unlink(name)

    def test_human_is_readonly_and_sane(self):
        self.assertEqual(cronos.human(512), "512 B")
        self.assertIn("KiB", cronos.human(2048))
        self.assertIn("MiB", cronos.human(3 * 1024 * 1024))

    def test_root_is_the_repo(self):
        self.assertTrue((cronos.ROOT / "README.md").is_file())
        self.assertTrue((cronos.ROOT / "tools" / "cronos.py").is_file())

    def test_shell_detection_does_not_raise(self):
        # None is a valid answer on Windows without Git Bash.
        sh = cronos.shell()
        self.assertTrue(sh is None or isinstance(sh, str))

    def test_git_rev_never_raises(self):
        self.assertTrue(cronos.git_rev())  # "nogit" is a valid answer


class TestDoctor(unittest.TestCase):
    def test_doctor_exits_zero_and_reports(self):
        buf = io.StringIO()
        stdout, sys.stdout = sys.stdout, buf
        try:
            rc = cronos.main(["doctor"])
        finally:
            sys.stdout = stdout
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("cronos-min doctor", out)
        self.assertIn("host checks", out)
        # It must name the OS it thinks it is on, not a hardcoded one.
        self.assertIn(cronos.platform.system(), out)


class TestRelease(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cronos-release-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _release(self, version="9.9.9-test"):
        buf = io.StringIO()
        stdout, sys.stdout = sys.stdout, buf
        try:
            rc = cronos.main(["release", "--version", version, "--out", self.tmp])
        finally:
            sys.stdout = stdout
        self.assertEqual(rc, 0, buf.getvalue())
        return Path(self.tmp) / f"cronos-min-{version}"

    def test_pack_layout_and_checksums(self):
        stage = self._release()
        self.assertTrue((stage / "RELEASE.md").is_file())
        self.assertTrue((stage / "SHA256SUMS.txt").is_file())
        self.assertTrue((stage / "README.md").is_file())
        self.assertTrue((stage / "docs" / "BUILD.md").is_file())
        self.assertTrue((stage / "tools" / "cronos.py").is_file())

    def test_checksum_file_matches_the_files(self):
        # A checksum file that does not match its own tree is worse than none.
        stage = self._release()
        lines = (stage / "SHA256SUMS.txt").read_text().splitlines()
        self.assertTrue(lines)
        for line in lines:
            digest, rel = line.split("  ", 1)
            target = stage / rel
            self.assertTrue(target.is_file(), f"{rel} listed but missing")
            with open(target, "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), digest, rel)

    def test_tarball_and_zip_contain_the_same_names(self):
        version = "9.9.9-test"
        stage = self._release(version)
        tarball = Path(self.tmp) / f"cronos-min-{version}.tar.gz"
        zippath = Path(self.tmp) / f"cronos-min-{version}.zip"
        with tarfile.open(tarball) as tf:
            tar_names = {n.split("/", 1)[1] for n in tf.getnames() if "/" in n}
        with zipfile.ZipFile(zippath) as zf:
            zip_names = {n.split("/", 1)[1] for n in zf.namelist() if "/" in n}
        self.assertEqual(tar_names, zip_names)
        self.assertIn("RELEASE.md", tar_names)
        self.assertIn("SHA256SUMS.txt", tar_names)

    def test_pack_excludes_git_and_build_output(self):
        stage = self._release()
        for bad in (".git", "build", "dist", "kernel/src", "__pycache__"):
            self.assertFalse((stage / bad).exists(), f"{bad} leaked into the pack")

    def test_release_notes_state_which_artifacts_are_missing(self):
        stage = self._release()
        notes = (stage / "RELEASE.md").read_text()
        # Either the images are present, or the notes say they are not built.
        images = stage / "images"
        if not images.is_dir() or not any(images.iterdir()):
            self.assertIn("not built", notes)
        self.assertIn("Unsigned", notes)

    def test_json_manifest_is_parseable(self):
        import json
        self._release()
        data = json.loads((Path(self.tmp) / "release.json").read_text())
        self.assertEqual(data["version"], "9.9.9-test")
        self.assertIn("packs", data)
        self.assertTrue(all(v["size"] > 0 for v in data["packs"].values()))


if __name__ == "__main__":
    unittest.main()
