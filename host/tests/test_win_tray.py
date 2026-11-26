"""Tests for the Windows tray controller's daemon lifecycle helpers."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HOST_DIR = Path(__file__).resolve().parents[1]
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))

import win_tray  # noqa: E402


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminate_calls = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = 0

    def wait(self, timeout=None):
        self.returncode = 0
        return self.returncode

    def kill(self):
        self.returncode = -9


class WindowsTrayControllerTests(unittest.TestCase):
    def test_loads_saved_deckd_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "args.json"
            path.write_text(json.dumps(["--port", "COM4", "--no-wifi"]), encoding="utf-8")
            self.assertEqual(win_tray.load_deckd_args(path), ["--port", "COM4", "--no-wifi"])

    def test_ignores_invalid_saved_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "args.json"
            path.write_text('{"port": "COM4"}', encoding="utf-8")
            self.assertEqual(win_tray.load_deckd_args(path), [])

    def test_start_is_idempotent_and_stop_terminates_daemon(self) -> None:
        process = FakeProcess()
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "deckd.log"
            popen = mock.Mock(return_value=process)
            controller = win_tray.DeckdController(
                Path("python.exe"), ["--port", "COM4"], log_path, popen
            )

            controller.start()
            controller.start()

            popen.assert_called_once()
            command = popen.call_args.args[0]
            self.assertEqual(command[0], "python.exe")
            self.assertEqual(command[1], str(win_tray.DECKD_SCRIPT))
            self.assertEqual(command[2:], ["--port", "COM4"])
            self.assertTrue(controller.running)

            controller.stop()
            self.assertFalse(controller.running)
            self.assertEqual(process.terminate_calls, 1)
            self.assertIn("Starting deckd", log_path.read_text(encoding="utf-8"))

    def test_open_gui_launches_the_profile_editor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "deckd.log"
            gui_log = Path(directory) / "deckgui.log"
            popen = mock.Mock()
            controller = win_tray.DeckdController(
                Path("python.exe"), [], log_path, popen
            )
            with mock.patch.object(win_tray, "GUI_LOG_PATH", gui_log):
                controller.open_gui()

            popen.assert_called_once()
            self.assertEqual(
                popen.call_args.args[0],
                ["pythonw.exe", str(win_tray.GUI_SCRIPT)],
            )
            self.assertIn("Starting deckgui", gui_log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
