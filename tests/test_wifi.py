"""Tests for the first-boot Wi-Fi provisioning path.

The rootfs is a read-only squashfs and there is no UI on first boot, so
credentials live in /data/wifi.conf and are turned into a tmpfs supplicant config
by rootfs/overlay/usr/sbin/vela-wifi-config. These tests run that script for
real, with the paths redirected into a temp directory.

What matters here: a wrong PSK must fail loudly instead of producing a
supplicant that silently never associates, and the secret must not appear in any
output the boot log would capture.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "rootfs", "overlay", "usr", "sbin", "vela-wifi-config")

TEMPLATE = """ctrl_interface=DIR=/run/wpa_supplicant GROUP=root
update_config=0
ap_scan=1
"""


class WifiConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vela-wifi-")
        self.conf = os.path.join(self.tmp, "wifi.conf")
        self.out = os.path.join(self.tmp, "wpa_supplicant.conf")
        self.tmpl = os.path.join(self.tmp, "template.conf")
        with open(self.tmpl, "w") as f:
            f.write(TEMPLATE)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self, conf=None):
        return dict(os.environ,
                    WIFI_CONF=conf or self.conf,
                    WIFI_OUT=self.out,
                    WIFI_TEMPLATE=self.tmpl)

    def _run(self, text):
        with open(self.conf, "w") as f:
            f.write(text)
        return subprocess.run(["sh", SCRIPT], capture_output=True,
                              text=True, env=self._env())

    def _out(self):
        with open(self.out) as f:
            return f.read()

    def test_passphrase_becomes_a_psk_network(self):
        r = self._run("SSID=HomeNet\nPSK=correct horse\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = self._out()
        self.assertIn("ctrl_interface=DIR=/run/wpa_supplicant", out)
        self.assertIn('ssid="HomeNet"', out)
        self.assertIn('psk="correct horse"', out)
        self.assertIn("key_mgmt=WPA-PSK", out)

    @unittest.skipIf(os.name == "nt",
                     "Windows chmod cannot express 0600 (it only toggles the "
                     "read-only bit); the file is written by the on-device helper")
    def test_output_is_root_only(self):
        self.assertEqual(self._run("SSID=HomeNet\nPSK=correct horse\n").returncode, 0)
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o600)

    def test_hex_pmk_is_written_unquoted(self):
        hexpsk = "0123456789abcdef" * 4
        r = self._run("SSID=HomeNet\nPSK=%s\n" % hexpsk)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("psk=%s\n" % hexpsk, self._out())

    def test_missing_psk_fails_and_writes_nothing(self):
        r = self._run("SSID=HomeNet\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("PSK", r.stderr)
        self.assertFalse(os.path.exists(self.out),
                         "a failed run must not leave a supplicant config behind")

    def test_short_psk_is_rejected(self):
        r = self._run("SSID=HomeNet\nPSK=short\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("8-63", r.stderr)

    def test_missing_conf_is_reported_as_unprovisioned(self):
        env = self._env(conf=os.path.join(self.tmp, "absent.conf"))
        r = subprocess.run(["sh", SCRIPT], capture_output=True, text=True, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no Wi-Fi credentials", r.stderr)

    def test_quotes_and_backslashes_in_ssid_are_escaped(self):
        r = self._run('SSID=My "Net"\\x\nPSK=correct horse\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('ssid="My \\"Net\\"\\\\x"', self._out())

    def test_crlf_and_quoted_values_from_a_windows_host(self):
        r = self._run('SSID="HomeNet"\r\nPSK="correct horse"\r\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        out = self._out()
        self.assertIn('ssid="HomeNet"', out)
        self.assertIn('psk="correct horse"', out)

    def test_hidden_network_sets_scan_ssid(self):
        r = self._run("SSID=HomeNet\nPSK=correct horse\nHIDDEN=1\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("scan_ssid=1", self._out())

    def test_secret_never_reaches_stdout_or_stderr(self):
        r = self._run("SSID=HomeNet\nPSK=correct horse\n")
        self.assertNotIn("correct horse", r.stdout)
        self.assertNotIn("correct horse", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
