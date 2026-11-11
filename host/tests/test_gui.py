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

"""Tests for the QtPy Stream Deck Settings & Profile Editor (deckgui)."""

import json
import tempfile
import unittest
from pathlib import Path
import tomllib
import tomli_w

# Ensure offscreen Qt
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["XDG_CURRENT_DESKTOP"] = "Generic"
os.environ["QT_QPA_PLATFORMTHEME"] = "generic"

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qtpy import QtWidgets
import deckgui
from deckgui import (
    AppPickerDialog,
    DeckMainWindow,
    SEMANTIC_COLORS,
    format_shortcut_badge,
    load_settings,
    save_settings,
)
from desktop_apps import DesktopApp


SAMPLE_TOML = """
[app.default]
[[app.default.groups]]
title = "Quick Launch"
buttons = [
  { label = "Term", shell = "x-terminal-emulator", icon = "terminal", color = "warn" },
  { label = "Files", shell = "nautilus", icon = "folder", color = "accent" }
]

[[app.default.groups]]
title = "Media"
buttons = [
  { label = "Play", keys = "play", icon = "play", color = "good" }
]

[app.code]
lock_target = true
[[app.code.groups]]
title = "Edit"
buttons = [
  { label = "Save", keys = "ctrl+s", icon = "save", color = "good" }
]
[[app.code.groups]]
title = "Views"
buttons = []
"""


class TestDeckGui(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.profiles_path = Path(self.tmp_dir.name) / "profiles.toml"
        self.profiles_path.write_text(SAMPLE_TOML, encoding="utf-8")
        self._orig_settings_file = deckgui.SETTINGS_FILE
        deckgui.SETTINGS_FILE = Path(self.tmp_dir.name) / "settings.json"

    def tearDown(self):
        deckgui.SETTINGS_FILE = self._orig_settings_file
        self.tmp_dir.cleanup()

    def test_load_and_parse_profiles(self):
        win = DeckMainWindow(profiles_path=self.profiles_path)
        self.assertIn("default", win.profiles_data.get("app", {}))
        self.assertIn("code", win.profiles_data.get("app", {}))
        self.assertEqual(win.combo_profiles.count(), 2)

    def test_button_data_retrieval(self):
        win = DeckMainWindow(profiles_path=self.profiles_path)
        win.current_app_id = "default"
        win.render_current_profile()

        btn0 = win.get_button_data(0)
        self.assertEqual(btn0.get("label"), "Term")
        self.assertEqual(btn0.get("shell"), "x-terminal-emulator")
        self.assertEqual(btn0.get("icon"), "terminal")
        self.assertEqual(btn0.get("color"), "warn")

        # Second group starts at index 8
        btn8 = win.get_button_data(8)
        self.assertEqual(btn8.get("label"), "Play")
        self.assertEqual(btn8.get("keys"), "play")

    def test_edit_button_and_save(self):
        win = DeckMainWindow(profiles_path=self.profiles_path)
        win.current_app_id = "default"
        win.render_current_profile()

        # Update button 1 (Files)
        win.select_button(1)
        win.ins_label.setText("Documents")
        win.ins_action_val.setText("nautilus ~/Documents")
        win.ins_icon.setCurrentText("folder")
        win.ins_color.setCurrentText("cyan")
        win.on_inspector_changed()

        win.save_and_apply()

        # Verify file on disk
        with open(self.profiles_path, "rb") as f:
            saved = tomllib.load(f)
        b1 = saved["app"]["default"]["groups"][0]["buttons"][1]
        self.assertEqual(b1["label"], "Documents")
        self.assertEqual(b1["icon"], "folder")

    def test_new_and_duplicate_profile(self):
        win = DeckMainWindow(profiles_path=self.profiles_path)
        self.assertEqual(len(win.profiles_data["app"]), 2)

        # Duplicate
        win.current_app_id = "code"
        win.profiles_data["app"]["code_copy"] = dict(win.profiles_data["app"]["code"])
        win.refresh_profile_list()
        self.assertIn("code_copy", win.profiles_data["app"])

    def test_copy_paste_button(self):
        win = DeckMainWindow(profiles_path=self.profiles_path)
        win.current_app_id = "default"
        win.render_current_profile()

        # Copy button 0 (Term)
        win.copy_button_slot(0)
        self.assertIsNotNone(win.clipboard_data)
        self.assertEqual(win.clipboard_data.get("label"), "Term")

        # Paste to button 5
        win.paste_button_slot(5)
        btn5 = win.get_button_data(5)
        self.assertEqual(btn5.get("label"), "Term")
        self.assertEqual(btn5.get("shell"), "x-terminal-emulator")

    def test_group_color_handling(self):
        win = DeckMainWindow(profiles_path=self.profiles_path)
        win.current_app_id = "default"
        win.render_current_profile()

        win.combo_g1_color.setCurrentText("purple")
        win.on_g1_color_changed("purple")
        self.assertEqual(win.profiles_data["app"]["default"]["groups"][0]["color"], "purple")

    def test_shortcut_badge_formatting(self):
        self.assertEqual(format_shortcut_badge({"keys": "ctrl+s"}), "^S")
        self.assertEqual(format_shortcut_badge({"shell": "nautilus"}), "SH")
        self.assertEqual(format_shortcut_badge({"keys": "f5"}), "F5")
        self.assertEqual(format_shortcut_badge({}), "")

    def test_shortcut_validation(self):
        win = DeckMainWindow(profiles_path=self.profiles_path)
        win.ins_action_type.setCurrentIndex(0)
        win.ins_action_val.setText("ctrl+s")
        win.validate_shortcut_input()
        self.assertIn("Valid", win.lbl_shortcut_valid.text())

        win.ins_action_val.setText("ctrl+nonexistent_token_xyz")
        win.validate_shortcut_input()
        self.assertIn("Unknown token", win.lbl_shortcut_valid.text())


    def test_multipage_groups_navigation_and_editing(self):
        win = DeckMainWindow(profiles_path=self.profiles_path)
        win.current_app_id = "default"
        win.render_current_profile()
        self.assertEqual(win.get_total_pages(), 1)

        # Add a new page (adds Group 3 and Group 4)
        win.add_new_page()
        self.assertEqual(win.get_total_pages(), 2)
        self.assertEqual(win.current_page, 1)
        self.assertIn("Page 2 of 2", win.lbl_page_indicator.text())

        # Set button on page 2 (Row 1 on Page 2 is Group 3, idx 0)
        win.select_button(0)
        win.ins_label.setText("Page2Btn")
        win.ins_action_val.setText("f12")
        win.on_inspector_changed()

        # Check that it wrote to group index 2 (Group 3)
        g3 = win.profiles_data["app"]["default"]["groups"][2]
        self.assertEqual(g3["buttons"][0]["label"], "Page2Btn")
        self.assertEqual(g3["buttons"][0]["keys"], "f12")

        # Navigate back to page 1
        win.prev_page()
        self.assertEqual(win.current_page, 0)
        self.assertIn("Page 1 of 2", win.lbl_page_indicator.text())
        # Check button 0 on Page 1 is still "Term"
        self.assertEqual(win.get_button_data(0).get("label"), "Term")

        # Save and verify on disk
        win.save_and_apply()
        with open(self.profiles_path, "rb") as f:
            saved = tomllib.load(f)
        self.assertEqual(len(saved["app"]["default"]["groups"]), 4)

    def test_idle_status_opens_choose_connection_tab_and_selects_modes(self):
        from unittest.mock import Mock

        win = DeckMainWindow(profiles_path=self.profiles_path)
        win.dbus_iface = Mock()

        win._connect_tab_auto_checked = False
        win.tabs.setCurrentIndex(0)
        win.apply_deckd_status({
            "transport": "none",
            "active_profile": "default",
            "override": "",
            "focus_app": "",
            "focus_title": "",
            "connected": "0",
            "usb": "0",
            "wifi": "0",
            "ble": "0",
        })
        self.assertEqual(win.tabs.currentIndex(), win.connect_tab_index)
        self.assertFalse(win.chk_usb.isChecked())
        self.assertFalse(win.chk_wifi.isChecked())
        self.assertFalse(win.chk_ble.isChecked())
        self.assertIn("IDLE", win.lbl_connect_state.text())

        win.dbus_iface.call.reset_mock()
        win.select_connection_mode("wifi")
        calls = [c.args for c in win.dbus_iface.call.call_args_list if c.args[0].startswith("Set")]
        self.assertEqual(calls, [
            ("SetUsbEnabled", False),
            ("SetWifiEnabled", True),
            ("SetBleEnabled", False),
        ])

        win.dbus_iface.call.reset_mock()
        win.select_connection_mode("usb")
        calls = [c.args for c in win.dbus_iface.call.call_args_list if c.args[0].startswith("Set")]
        self.assertEqual(calls, [
            ("SetUsbEnabled", True),
            ("SetWifiEnabled", False),
            ("SetBleEnabled", False),
        ])

    def test_launch_app_inspector_and_badge(self):
        self.assertEqual(format_shortcut_badge({"app": "org.gnome.Calculator.desktop"}), "APP")
        win = DeckMainWindow(profiles_path=self.profiles_path)
        win.current_app_id = "default"
        win.render_current_profile()
        win.select_button(3)
        win.ins_action_type.setCurrentIndex(2)
        win.ins_action_val.setText("firefox.desktop")
        win.ins_label.setText("Browser")
        win.on_inspector_changed()
        data = win.get_button_data(3)
        self.assertEqual(data.get("app"), "firefox.desktop")
        self.assertNotIn("keys", data)
        self.assertNotIn("shell", data)
        self.assertIn("installed Linux app", win.lbl_shortcut_valid.text())

    def test_launcher_tab_add_reorder_auto_return_and_save(self):
        sample_apps = [
            DesktopApp("firefox.desktop", "Firefox", "firefox %u", "Web Browser", categories=("Network", "WebBrowser")),
            DesktopApp("org.gnome.Calculator.desktop", "Calculator", "gnome-calculator", "Math", categories=("Utility",)),
        ]
        orig_discover = deckgui.discover_desktop_apps
        orig_suggest = deckgui.suggest_smart_defaults
        deckgui.discover_desktop_apps = lambda: sample_apps
        deckgui.suggest_smart_defaults = lambda apps: [{"id": "firefox.desktop", "label": "Firefox", "icon": "globe", "color": "accent"}]
        try:
            win = DeckMainWindow(profiles_path=self.profiles_path)
            self.assertEqual(win.list_launcher_available.count(), 2)
            self.assertEqual(win.list_launcher_selected.count(), 1)
            self.assertTrue(win.chk_launcher_auto_return.isChecked())

            # Select Calculator in available list and add it
            win.list_launcher_available.setCurrentRow(1)
            win.add_selected_to_launcher()
            self.assertEqual(win.list_launcher_selected.count(), 2)

            # Toggle auto_return off and move second item up
            win.chk_launcher_auto_return.setChecked(False)
            win.list_launcher_selected.setCurrentRow(1)
            win.move_launcher_item(-1)
            win.save_and_apply()

            with open(self.profiles_path, "rb") as f:
                saved = tomllib.load(f)
            self.assertFalse(saved["launcher"]["auto_return"])
            self.assertEqual([a["id"] for a in saved["launcher"]["apps"]], [
                "org.gnome.Calculator.desktop",
                "firefox.desktop",
            ])

            # Also verify AppPickerDialog filters properly
            dlg = AppPickerDialog(sample_apps, win)
            dlg.edit_search.setText("calc")
            self.assertEqual(dlg.list_apps.count(), 1)
        finally:
            deckgui.discover_desktop_apps = orig_discover
            deckgui.suggest_smart_defaults = orig_suggest


if __name__ == "__main__":
    unittest.main()
