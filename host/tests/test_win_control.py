"""Tests for the Windows daemon control protocol."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deckcontrol import DeckControl
from win_control import WindowsControlClient, WindowsControlServer


class WindowsControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.control = DeckControl(
            {"default": object(), "code": object()},
            usb_enabled=True,
            wifi_enabled=False,
        )
        self.control._is_windows = True
        self.server = WindowsControlServer(self.control, port=0)
        self.server.start()
        self.client = WindowsControlClient(port=self.server.port)

    def tearDown(self) -> None:
        self.server.close()

    def test_reads_status_and_profile_ids(self) -> None:
        self.assertEqual(self.client.call("ListProfiles"), ["code", "default"])
        status = self.client.call("GetStatus")
        self.assertEqual(status["usb"], "1")
        self.assertEqual(status["ble"], "0")

    def test_changes_override_and_transport_state(self) -> None:
        self.client.call("SetOverride", "code")
        self.client.call("SetWifiEnabled", True)
        status = self.client.call("GetStatus")
        self.assertEqual(status["override"], "code")
        self.assertEqual(status["wifi"], "1")

    def test_rejects_unknown_profiles_and_windows_ble(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unknown profile"):
            self.client.call("SetOverride", "missing")
        with self.assertRaisesRegex(RuntimeError, "BLE is not supported"):
            self.client.call("SetBleEnabled", True)
        self.assertEqual(self.client.call("GetStatus")["ble"], "0")

    def test_rejects_unknown_control_method(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unsupported control request"):
            self.client.call("RunCommand")


if __name__ == "__main__":
    unittest.main()
