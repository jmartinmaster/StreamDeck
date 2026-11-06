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
"""Device profile selection and shared tray override regression tests."""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deckd
from deckcontrol import DeckControl


class DeviceProfileTests(unittest.TestCase):
    def setUp(self):
        self.profiles = {
            "default": deckd.Profile("default", [deckd.Button("Term", shell="x-terminal-emulator")]),
            "code": deckd.Profile("code", [deckd.Button("Save", keys="ctrl+s")]),
        }
        self.control = DeckControl(self.profiles)
        self.port = Mock()
        self.session = deckd.DeckSession(self.port, self.profiles, Mock(),
                                         control=self.control, transport_name="ble")

    def sent(self):
        return b"".join(call.args[0] for call in self.port.write.call_args_list).decode()

    def test_handshake_catalog_and_legacy_prefix(self):
        self.session.handshake()
        lines = self.sent().splitlines()
        self.assertEqual(lines[:3], [
            "DECK HELLO v1", "DECK FOCUS default\tdeckd connected",
            "DECK LAYOUT 1/1\tTerm" + "\t" * 15])
        self.assertIn("DECK PROFILES BEGIN\nDECK PROFILE code\nDECK PROFILE default\nDECK PROFILES END\n",
                      self.sent())
        self.assertIn("DECK SELECTED\t\n", self.sent())
        self.assertIn("DECK HOST\t", self.sent())

    def test_device_hello_includes_host_identity(self):
        with patch.object(deckd.socket, "gethostname", return_value="desktop.local"):
            self.session.handle_line("DECK HELLO v1 panel")
        self.assertIn("DECK HOST\tdesktop\n", self.sent())

    def test_select_pin_press_and_restore_auto(self):
        self.session.set_focus("code", "Editor")
        self.session.handle_line("DECK SELECT\tdefault")
        self.assertEqual(self.control.override(), "default")
        self.assertEqual(self.session.active_app, "default")
        self.assertIn("DECK SELECTED\tdefault\n", self.sent())
        self.session.set_focus("code", "Another editor")
        self.assertEqual(self.session.active_app, "default")
        with patch.object(deckd.subprocess, "Popen") as popen:
            self.session.handle_line("DECK PRESS 0 42")
            self.assertEqual(popen.call_args.args[0], "x-terminal-emulator")
            self.assertNotIn("stderr", popen.call_args.kwargs)
        self.assertIn("DECK ACK 42\n", self.sent())
        self.session.handle_line("DECK SELECT")  # readline strips trailing tab for auto
        self.assertEqual(self.control.override(), "")
        self.assertEqual(self.session.active_app, "code")
        self.assertIn("DECK LAYOUT 1/1\tSave", self.sent())

    def test_tray_override_updates_device(self):
        self.session.set_focus("code", "Editor")
        self.control.set_override("default")
        self.session.tick()
        self.assertIn("DECK SELECTED\tdefault\n", self.sent())
        self.assertEqual(self.session.active_app, "default")
        self.control.set_override("")
        self.session.tick()
        self.assertEqual(self.session.active_app, "code")

    def test_unknown_selection_is_rejected(self):
        self.control.set_override("code")
        self.session.tick()
        self.session.handle_line("DECK SELECT\tmissing")
        self.assertEqual(self.control.override(), "code")
        self.assertIn("DECK ERR SELECT unknown profile\n", self.sent())
        self.assertIn("DECK SELECTED\tcode\n", self.sent())

    def test_catalog_bounds_are_explicit(self):
        self.profiles["x" * 64] = deckd.Profile("x" * 64)
        self.session.push_profiles()
        self.assertEqual(self.sent(), "DECK PROFILES ERROR\n")
        del self.profiles["x" * 64]
        for i in range(25):
            app = f"{i:02}" + "a" * 48
            self.profiles[app] = deckd.Profile(app)
        self.session.push_profiles()
        self.assertEqual(self.sent(), "DECK PROFILES ERROR\n" * 2)

    def test_profiles_reload_resets_deleted_pin(self):
        self.control.set_override("code")
        self.session.tick()
        self.session.profile_store = Mock()
        self.session.profile_store.maybe_reload.return_value = True
        del self.profiles["code"]
        self.session.tick()
        self.assertEqual(self.control.override(), "")
        self.assertEqual(self.session.active_app, "default")
        self.assertIn("DECK SELECTED\t\n", self.sent())

    def test_shipped_default_terminal_uses_system_alternative(self):
        source = Path(__file__).resolve().parents[1]
        profiles = deckd.load_profiles(source / "profiles.toml")
        terminal = next(button for button in profiles["default"].buttons if button.label == "Term")
        self.assertEqual(terminal.shell, "x-terminal-emulator")
        self.assertIn("org.gnome.Ptyxis", profiles)
        self.assertEqual(profiles["org.gnome.Ptyxis"].buttons[2].shell, "ptyxis --tab")


    def test_grouped_profile_layout_and_commands(self):
        g1 = deckd.Group("Views", [
            deckd.Button("Iso", keys="0", icon="cube", color="accent"),
            deckd.Button("Top", keys="2", icon="arrow-up", color="accent"),
        ])
        g2 = deckd.Group("Tools", [
            deckd.Button("Pad", keys="shift+p", icon="box", color="good"),
        ])
        cad = deckd.Profile("cad", groups=[g1, g2])
        self.profiles["cad"] = cad
        self.session.set_focus("cad", "FreeCAD 1.0")
        sent_lines = self.sent().splitlines()
        self.assertIn("DECK GROUPS\tViews\tTools", sent_lines)
        self.assertIn("DECK ICONS\tcube\tarrow-up\t\t\t\t\t\t\tbox\t\t\t\t\t\t\t", sent_lines)
        self.assertIn("DECK COLORS\taccent\taccent\t\t\t\t\t\t\tgood\t\t\t\t\t\t\t", sent_lines)
        layout = next(l for l in sent_lines if l.startswith("DECK LAYOUT 1/1"))
        parts = layout.split("\t")
        self.assertEqual(parts[1], "Iso")
        self.assertEqual(parts[2], "Top")
        self.assertEqual(parts[9], "Pad")  # Starts at index 8 (9th item including command header)


if __name__ == "__main__":
    unittest.main()
