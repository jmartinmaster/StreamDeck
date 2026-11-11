#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This file is part of Stream Deck.
#
# Stream Deck is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Stream Deck is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Stream Deck.  If not, see <https://www.gnu.org/licenses/>.
"""Rootless package build and extracted payload checks."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1]
PACKAGING = SOURCE / "packaging"


class PackageTests(unittest.TestCase):
    def test_build_and_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(
                ["bash", str(PACKAGING / "build-deb.sh"), "1.0.0", str(root)],
                check=True, capture_output=True, text=True)
            package = root / "mmesp32-deckd_1.0.0_amd64.deb"
            metadata = subprocess.check_output(
                ["dpkg-deb", "--field", str(package)], text=True)
            self.assertIn("Architecture: amd64", metadata)
            self.assertIn("python3-dbus-next", metadata)
            self.assertIn("python3-evdev", metadata)
            self.assertIn("x-terminal-emulator", metadata)
            self.assertNotIn(str(Path.home()), metadata)
            payload = root / "payload"
            subprocess.run(["dpkg-deb", "--extract", str(package), str(payload)], check=True)
            files = {str(path.relative_to(payload)) for path in payload.rglob("*")
                     if path.is_file()}
            self.assertEqual(len(files), 17)
            self.assertFalse(any(".venv" in path or "__pycache__" in path for path in files))
            for command in ("deckd", "decktray", "deckd-user"):
                path = payload / "usr/bin" / command
                self.assertTrue(os.access(path, os.X_OK))
                subprocess.run(["sh", "-n", str(path)], check=True)
            for module in ("deckd", "decktray"):
                result = subprocess.run(
                    [sys.executable, str(payload / "usr/lib/mmesp32-deckd" / (module + ".py")), "--help"],
                    check=True, capture_output=True, text=True)
                self.assertIn("usage:", result.stdout)
            profiles = root / "custom-profiles.toml"
            profiles.write_text("[app.default]\nbuttons = []\n")
            environment = dict(os.environ, DECKD_PROFILES=str(profiles))
            result = subprocess.run(
                [sys.executable, "-c",
                 "import decktray; print(decktray.PROFILES_TOML)"],
                cwd=payload / "usr/lib/mmesp32-deckd", env=environment,
                check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), str(profiles))
            desktop = payload / "usr/share/applications/mmesp32-decktray.desktop"
            subprocess.run(["desktop-file-validate", str(desktop)], check=True)
            unit = (payload / "usr/lib/systemd/user/deckd.service").read_text()
            self.assertIn("WantedBy=graphical-session.target", unit)
            self.assertNotIn(".venv", unit)
            control = root / "control"
            subprocess.run(["dpkg-deb", "--control", str(package), str(control)], check=True)
            subprocess.run(["sh", "-n", str(control / "postinst")], check=True)

    def test_user_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = root / "bin"
            fake.mkdir()
            calls = root / "calls"
            stub = '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n'
            for name in ("busctl", "systemctl"):
                command = fake / name
                command.write_text(stub)
                command.chmod(0o755)
            environment = dict(os.environ, PATH=str(fake) + ":" + os.environ["PATH"],
                               CALLS=str(calls), XDG_CONFIG_HOME=str(root / "config"))
            for action, expected in (("release-usb", "false"), ("resume-usb", "true")):
                subprocess.run(["sh", str(PACKAGING / "deckd-user"), action],
                               env=environment, check=True)
                self.assertIn("SetUsbEnabled b " + expected, calls.read_text())
            subprocess.run(["sh", str(PACKAGING / "deckd-user"), "disable"],
                           env=environment, check=True)
            self.assertIn("disable --now decktray.service deckd.service", calls.read_text())
            (root / "config/systemd/user").mkdir(parents=True)
            (root / "config/systemd/user/deckd.service").write_text("existing")
            result = subprocess.run(["sh", str(PACKAGING / "deckd-user"), "enable"],
                                    env=environment, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("shadows the packaged", result.stderr)

    def test_invalid_version_is_rejected(self):
        result = subprocess.run(["bash", str(PACKAGING / "build-deb.sh"), "../invalid"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
