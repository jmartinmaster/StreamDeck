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
"""Unit tests for portable Linux XDG App Launcher discovery, execution, and protocol."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import deckd
import desktop_apps
from deckcontrol import DeckControl


def write_desktop(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[Desktop Entry]\nType=Application\n" + body, encoding="utf-8")
    return path


class DesktopAppsTests(unittest.TestCase):
    def test_xdg_discovery_filtering_and_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_apps = root / "user_apps"
            sys_apps = root / "sys_apps"

            # User override of firefox.desktop wins over system entry
            write_desktop(
                sys_apps,
                "firefox.desktop",
                "Name=System Firefox\nExec=firefox %u\nCategories=Network;WebBrowser;\n",
            )
            write_desktop(
                user_apps,
                "firefox.desktop",
                "Name=Custom Firefox\nExec=firefox --new-window %U\nCategories=Network;WebBrowser;\n",
            )
            # Nested reverse-DNS id works (sub/app -> sub-app.desktop)
            write_desktop(
                sys_apps,
                "org/example/Tool.desktop",
                "Name=Example Tool\nExec=/usr/bin/true %f\n",
            )
            # Filtered entries
            write_desktop(sys_apps, "hidden.desktop", "Name=Hidden\nExec=true\nHidden=true\n")
            write_desktop(sys_apps, "nodisp.desktop", "Name=NoDisp\nExec=true\nNoDisplay=true\n")
            write_desktop(sys_apps, "missingbin.desktop", "Name=Missing\nTryExec=/no/such/binary\nExec=true\n")
            write_desktop(sys_apps, "kdeonly.desktop", "Name=KDE\nExec=true\nOnlyShowIn=KDE;\n")

            apps = desktop_apps.discover_desktop_apps(
                app_dirs=[user_apps, sys_apps],
                current_desktop="GNOME",
                check_try_exec=True,
            )
            by_id = {app.desktop_id: app for app in apps}
            self.assertIn("firefox.desktop", by_id)
            self.assertEqual(by_id["firefox.desktop"].name, "Custom Firefox")
            self.assertIn("org-example-Tool.desktop", by_id)
            self.assertNotIn("hidden.desktop", by_id)
            self.assertNotIn("nodisp.desktop", by_id)
            self.assertNotIn("missingbin.desktop", by_id)
            self.assertNotIn("kdeonly.desktop", by_id)

    def test_build_exec_argv_strips_field_codes(self):
        app = desktop_apps.DesktopApp(
            "demo.desktop",
            "Demo",
            'env FOO="a b" /usr/bin/app --open %U @@u --next %f @@',
        )
        argv = desktop_apps.build_exec_argv(app)
        self.assertEqual(argv, ["env", "FOO=a b", "/usr/bin/app", "--open", "--next"])

    def test_launch_desktop_app_prefers_gio_then_gtk_then_exec(self):
        with tempfile.TemporaryDirectory() as tmp:
            desktop_file = write_desktop(
                Path(tmp),
                "demo.desktop",
                "Name=Demo\nExec=/usr/bin/demo --flag %U\n",
            )
            app = desktop_apps.DesktopApp("demo.desktop", "Demo", "/usr/bin/demo --flag %U", path=desktop_file)
            apps_by_id = {"demo.desktop": app}

            with patch.object(desktop_apps.shutil, "which", side_effect=lambda cmd: f"/usr/bin/{cmd}" if cmd == "gio" else None), \
                 patch.object(desktop_apps.subprocess, "Popen") as popen:
                desktop_apps.launch_desktop_app("demo", apps_by_id)
                self.assertEqual(popen.call_args.args[0], ["gio", "launch", str(desktop_file)])

            with patch.object(desktop_apps.shutil, "which", side_effect=lambda cmd: f"/usr/bin/{cmd}" if cmd == "gtk-launch" else None), \
                 patch.object(desktop_apps.subprocess, "Popen") as popen:
                desktop_apps.launch_desktop_app("demo.desktop", apps_by_id)
                self.assertEqual(popen.call_args.args[0], ["gtk-launch", "demo.desktop"])

            with patch.object(desktop_apps.shutil, "which", return_value=None), \
                 patch.object(desktop_apps.subprocess, "Popen") as popen:
                desktop_apps.launch_desktop_app("demo.desktop", apps_by_id)
                self.assertEqual(popen.call_args.args[0], ["/usr/bin/demo", "--flag"])

    def test_suggest_smart_defaults_uses_xdg_mime(self):
        apps = [
            desktop_apps.DesktopApp("firefox.desktop", "Firefox", "firefox", categories=("Network", "WebBrowser")),
            desktop_apps.DesktopApp("org.gnome.Nautilus.desktop", "Files", "nautilus", categories=("System", "FileManager")),
            desktop_apps.DesktopApp("org.gnome.Ptyxis.desktop", "Terminal", "ptyxis", categories=("System", "TerminalEmulator")),
            desktop_apps.DesktopApp("code.desktop", "Visual Studio Code", "code", categories=("Development", "IDE")),
        ]
        mime_map = {
            "x-scheme-handler/https": "firefox.desktop",
            "inode/directory": "org.gnome.Nautilus.desktop",
        }
        defaults = desktop_apps.suggest_smart_defaults(apps, xdg_mime_query=mime_map.get, limit=10)
        self.assertEqual([d["id"] for d in defaults], [
            "firefox.desktop",
            "org.gnome.Nautilus.desktop",
            "org.gnome.Ptyxis.desktop",
            "code.desktop",
        ])
        self.assertEqual(defaults[0]["icon"], "globe")
        self.assertEqual(defaults[1]["icon"], "folder")
        self.assertEqual(defaults[2]["icon"], "terminal")

    def test_deckd_launcher_protocol_and_app_button(self):
        sample_toml = """
[app.default]
[[app.default.groups]]
title = "Apps"
buttons = [
  { label = "LaunchFF", app = "firefox.desktop", icon = "globe", color = "accent" }
]

[launcher]
auto_return = false
[[launcher.apps]]
id = "firefox.desktop"
label = "Firefox"
icon = "globe"
color = "accent"
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profiles.toml"
            path.write_text(sample_toml, encoding="utf-8")
            with patch.object(deckd, "discover_desktop_apps", return_value=[]):
                profiles, launcher = deckd.load_profiles_and_launcher(path)

        self.assertFalse(launcher.auto_return)
        self.assertEqual(len(launcher.apps), 1)
        self.assertEqual(profiles["default"].buttons[0].app, "firefox.desktop")

        port = Mock()
        control = DeckControl(profiles)
        session = deckd.DeckSession(
            port,
            profiles,
            Mock(),
            control=control,
            transport_name="usb",
            launcher=launcher,
        )
        session.handshake()
        sent = b"".join(c.args[0] for c in port.write.call_args_list).decode()
        self.assertIn("DECK LAUNCHER BEGIN\t0\t1\n", sent)
        self.assertIn("DECK LAUNCHER ITEM\t0\tFirefox\tglobe\taccent\n", sent)
        self.assertIn("DECK LAUNCHER END\n", sent)

        port.write.reset_mock()
        with patch.object(deckd, "discover_desktop_apps", return_value=[]), \
             patch.object(deckd, "launch_desktop_app") as launcher_fn:
            session.handle_line("DECK LAUNCHER PRESS 0 7")
            launcher_fn.assert_called_once()
            self.assertEqual(launcher_fn.call_args.args[0], "firefox.desktop")
            self.assertIn(b"DECK ACK 7\n", port.write.call_args.args[0])

            # Invalid index returns DECK ERR
            port.write.reset_mock()
            session.handle_line("DECK LAUNCHER PRESS 99 8")
            self.assertIn(b"DECK ERR 8 no launcher app at index 99\n", port.write.call_args.args[0])

            # Profile button with app= launches desktop app too
            launcher_fn.reset_mock()
            port.write.reset_mock()
            session.handle_line("DECK PRESS 0 9")
            launcher_fn.assert_called_once()
            self.assertEqual(launcher_fn.call_args.args[0], "firefox.desktop")
            self.assertIn(b"DECK ACK 9\n", port.write.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
