# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Windows bare-minimum support (win_platform + deckd wiring)."""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

HOST_DIR = Path(__file__).resolve().parent.parent
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))

import deckd  # noqa: E402
import win_platform  # noqa: E402


class FakeUser32:
    """In-memory mock of Win32 user32.SendInput and MapVirtualKeyW."""

    def __init__(self) -> None:
        self.calls: list[list[tuple[int, int, int]]] = []

    def MapVirtualKeyW(self, vk: int, mode: int) -> int:
        return (vk + 0x10) & 0xFF

    def SendInput(self, count: int, inputs, size: int) -> int:
        assert size == ctypes.sizeof(win_platform._INPUT)
        batch = []
        for i in range(count):
            ki = inputs[i].union.ki
            batch.append((int(ki.wVk), int(ki.wScan), int(ki.dwFlags)))
        self.calls.append(batch)
        return count


class WinKeyMappingTests(unittest.TestCase):
    def test_input_struct_size_matches_win32_abi(self) -> None:
        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(ctypes.sizeof(win_platform._INPUT), expected)

    def test_modifiers_and_letters(self) -> None:
        self.assertEqual(win_platform.key_token_to_vk("ctrl"), (0xA2, False))
        self.assertEqual(win_platform.key_token_to_vk("rctrl"), (0xA3, True))
        self.assertEqual(win_platform.key_token_to_vk("super"), (0x5B, True))
        self.assertEqual(win_platform.key_token_to_vk("win"), (0x5B, True))
        self.assertEqual(win_platform.key_token_to_vk("c"), (ord("C"), False))
        self.assertEqual(win_platform.key_token_to_vk("5"), (ord("5"), False))

    def test_function_and_media_keys(self) -> None:
        self.assertEqual(win_platform.key_token_to_vk("f1"), (0x70, False))
        self.assertEqual(win_platform.key_token_to_vk("f12"), (0x7B, False))
        self.assertEqual(win_platform.key_token_to_vk("f24"), (0x87, False))
        self.assertEqual(win_platform.key_token_to_vk("volup"), (0xAF, True))
        self.assertEqual(win_platform.key_token_to_vk("play"), (0xB3, True))
        self.assertEqual(win_platform.key_token_to_vk("kpenter"), (0x0D, True))

    def test_unknown_key_raises(self) -> None:
        with self.assertRaises(ValueError):
            win_platform.key_token_to_vk("not_a_real_key")
        with self.assertRaises(ValueError):
            win_platform.key_token_to_vk("f25")

    def test_send_chord_presses_in_order_and_releases_in_reverse(self) -> None:
        fake = FakeUser32()
        inj = win_platform.WinKeyInjector(user32=fake)
        self.assertTrue(inj.available)

        inj.send_chord("ctrl+shift+s", hold_s=0.0)
        self.assertEqual(len(fake.calls), 2)

        down, up = fake.calls
        self.assertEqual([ev[0] for ev in down], [0xA2, 0xA0, ord("S")])
        self.assertEqual([ev[0] for ev in up], [ord("S"), 0xA0, 0xA2])
        # All down events have KEYUP unset; all up events have KEYUP set.
        for _, _, flags in down:
            self.assertEqual(flags & win_platform.KEYEVENTF_KEYUP, 0)
        for _, _, flags in up:
            self.assertNotEqual(flags & win_platform.KEYEVENTF_KEYUP, 0)

    def test_send_chord_supports_multi_step_sequences(self) -> None:
        fake = FakeUser32()
        inj = win_platform.WinKeyInjector(user32=fake)
        inj.send_chord("ctrl+k,ctrl+c", hold_s=0.0)
        self.assertEqual(len(fake.calls), 4)
        self.assertEqual([ev[0] for ev in fake.calls[0]], [0xA2, ord("K")])
        self.assertEqual([ev[0] for ev in fake.calls[2]], [0xA2, ord("C")])

    def test_send_chord_sets_extended_flag(self) -> None:
        fake = FakeUser32()
        inj = win_platform.WinKeyInjector(user32=fake)
        inj.send_chord("super+left", hold_s=0.0)
        down = fake.calls[0]
        for _, _, flags in down:
            self.assertNotEqual(flags & win_platform.KEYEVENTF_EXTENDEDKEY, 0)

    def test_unlock_sequence_zeroizes_and_raises(self) -> None:
        inj = win_platform.WinKeyInjector(user32=FakeUser32())
        pw = bytearray(b"secret-pw")
        with self.assertRaises(RuntimeError):
            inj.unlock_sequence(pw)
        self.assertEqual(pw, bytearray(len(pw)))


class WinFocusWatcherTests(unittest.TestCase):
    def test_polls_and_deduplicates_focus_changes(self) -> None:
        states = iter([
            ("code", "deckd.py - Visual Studio Code"),
            ("code", "deckd.py - Visual Studio Code"),
            ("msedge", "GitHub - Microsoft Edge"),
        ])
        last = ("msedge", "GitHub - Microsoft Edge")

        def query() -> tuple[str, str]:
            nonlocal last
            try:
                last = next(states)
            except StopIteration:
                pass
            return last

        watcher = win_platform.WinFocusWatcher(poll_interval=0.01, query_fn=query)
        watcher.start()
        try:
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                cur = watcher.current()
                if cur == ("msedge", "GitHub - Microsoft Edge"):
                    break
                time.sleep(0.01)
            self.assertEqual(watcher.current(), ("msedge", "GitHub - Microsoft Edge"))
        finally:
            watcher.stop()


class SerialPortAutoDetectTests(unittest.TestCase):
    def test_prefers_known_esp32_vid(self) -> None:
        ports = [
            types.SimpleNamespace(device="COM1", vid=0x0001, description="Communications Port", manufacturer=""),
            types.SimpleNamespace(device="COM5", vid=0x303A, description="USB JTAG/serial debug unit", manufacturer="Espressif"),
        ]
        self.assertEqual(win_platform.find_default_serial_port(ports), "COM5")

    def test_falls_back_to_description_keyword(self) -> None:
        ports = [
            types.SimpleNamespace(device="COM1", vid=None, description="Communications Port", manufacturer=""),
            types.SimpleNamespace(device="COM7", vid=None, description="Silicon Labs CP210x USB to UART Bridge", manufacturer=""),
        ]
        self.assertEqual(win_platform.find_default_serial_port(ports), "COM7")

    def test_single_port_fallback(self) -> None:
        ports = [
            types.SimpleNamespace(device="COM3", vid=None, description="Generic Serial", manufacturer=""),
        ]
        self.assertEqual(win_platform.find_default_serial_port(ports), "COM3")

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(win_platform.find_default_serial_port([]))


class DeckdWindowsWiringTests(unittest.TestCase):
    def test_cli_help_formats_windows_unlock_path(self) -> None:
        result = subprocess.run(
            [sys.executable, str(HOST_DIR / "deckd.py"), "--help"],
            cwd=HOST_DIR,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("%LOCALAPPDATA%\\StreamDeck\\unlock.key", result.stdout)

    @unittest.skipUnless(sys.platform == "win32", "Windows app backend only")
    def test_windows_uses_start_menu_apps_and_disables_ble(self) -> None:
        import win_apps

        self.assertIs(deckd.discover_desktop_apps, win_apps.discover_windows_apps)
        self.assertFalse(deckd.HAVE_BLE)

    @unittest.skipUnless(sys.platform == "win32", "Windows app backend only")
    def test_device_launcher_dispatches_windows_app_id(self) -> None:
        app_id = "startmenu:user:Accessories/Calculator.lnk"
        launcher = deckd.LauncherConfig(apps=[deckd.Button(label="Calc", app=app_id)])
        session = deckd.DeckSession(None, {"default": deckd.Profile("default")}, None, launcher=launcher)
        with mock.patch.object(deckd, "launch_desktop_app") as launch:
            session.run_launcher_button(0)
        launch.assert_called_once_with(app_id)

    def test_windows_profiles_toml_parses_and_all_keys_valid(self) -> None:
        win_toml = HOST_DIR / "profiles.windows.toml"
        self.assertTrue(win_toml.is_file())
        profiles = deckd.load_profiles(win_toml)
        self.assertIn("default", profiles)
        self.assertIn("code", profiles)
        self.assertIn("browser", profiles)
        self.assertIn("windowsterminal", profiles)
        # Every key chord in profiles.windows.toml must map cleanly to Win32 VKs.
        for prof in profiles.values():
            for btn in prof.buttons:
                if btn.keys:
                    for token in btn.keys.split("+"):
                        vk, _ = win_platform.key_token_to_vk(token)
                        self.assertGreater(vk, 0)

    def test_resolve_serial_port(self) -> None:
        self.assertIsNone(deckd._resolve_serial_port("none"))
        self.assertIsNone(deckd._resolve_serial_port("NONE"))
        self.assertEqual(deckd._resolve_serial_port("COM4"), "COM4")
        with mock.patch.object(deckd, "find_default_serial_port", return_value="COM9"):
            self.assertEqual(deckd._resolve_serial_port("auto"), "COM9")

    def test_spawn_shell_command_uses_windows_flags_on_win32(self) -> None:
        with mock.patch.object(deckd.sys, "platform", "win32"), \
                mock.patch.object(deckd.subprocess, "Popen") as popen:
            deckd._spawn_shell_command("explorer")
            popen.assert_called_once()
            _, kwargs = popen.call_args
            self.assertTrue(kwargs["shell"])
            self.assertNotIn("start_new_session", kwargs)
            self.assertIn("creationflags", kwargs)
            self.assertNotEqual(kwargs["creationflags"], 0)


if __name__ == "__main__":
    unittest.main()
