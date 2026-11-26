"""Tests for Windows Start Menu app discovery and launch."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import win_apps


class WindowsAppsTests(unittest.TestCase):
    def test_discovers_scoped_shortcuts_and_ignores_startup_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user_menu = root / "user"
            common_menu = root / "common"
            for path in (
                user_menu / "Microsoft Edge.lnk",
                user_menu / "Startup" / "Auto launch.lnk",
                common_menu / "Accessories" / "Calculator.lnk",
                common_menu / "Uninstall" / "Remove app.lnk",
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")

            apps = win_apps.discover_windows_apps([
                ("user", user_menu),
                ("common", common_menu),
            ])

        by_id = {app.desktop_id: app for app in apps}
        self.assertIn("startmenu:user:Microsoft Edge.lnk", by_id)
        self.assertIn("startmenu:common:Accessories/Calculator.lnk", by_id)
        self.assertNotIn("startmenu:user:Startup/Auto launch.lnk", by_id)
        self.assertNotIn("startmenu:common:Uninstall/Remove app.lnk", by_id)

    def test_smart_defaults_prefers_common_windows_apps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            apps = []
            for name in ("Windows Terminal", "File Explorer", "Microsoft Edge"):
                shortcut = root / f"{name}.lnk"
                shortcut.write_text("", encoding="utf-8")
                apps.append(win_apps.WindowsApp(
                    desktop_id=f"startmenu:user:{shortcut.name}",
                    name=name,
                    path=shortcut,
                ))

            defaults = win_apps.suggest_smart_defaults(apps)

        self.assertEqual([entry["icon"] for entry in defaults[:3]], ["terminal", "folder", "globe"])
        self.assertEqual([entry["id"] for entry in defaults[:3]], [app.desktop_id for app in apps])

    def test_launch_uses_windows_shell_for_resolved_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            shortcut = Path(directory) / "Notepad.lnk"
            shortcut.write_text("", encoding="utf-8")
            app = win_apps.WindowsApp("startmenu:user:Notepad.lnk", "Notepad", path=shortcut)
            with patch.object(win_apps.os, "startfile", create=True) as startfile:
                win_apps.launch_desktop_app(app.desktop_id, {app.desktop_id: app})
            startfile.assert_called_once_with(str(shortcut))


if __name__ == "__main__":
    unittest.main()
