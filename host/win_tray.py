#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Windows notification-area controller for the Stream Deck daemon."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

HOST_DIR = Path(__file__).resolve().parent
DECKD_SCRIPT = HOST_DIR / "deckd.py"
APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "StreamDeck"
LOG_PATH = APP_DIR / "deckd.log"
GUI_LOG_PATH = APP_DIR / "deckgui.log"
ARGS_PATH = APP_DIR / "deckd-args.json"
GUI_SCRIPT = HOST_DIR / "deckgui.py"
MUTEX_NAME = "Local\\StreamDeckWindowsTray"
log = logging.getLogger("streamdeck.win_tray")


def load_deckd_args(path: Path = ARGS_PATH) -> list[str]:
    """Load optional daemon arguments saved by run-windows.ps1."""
    if not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        log.exception("Could not read daemon arguments from %s", path)
        return []
    if not isinstance(value, list) or not all(isinstance(arg, str) for arg in value):
        log.error("Ignoring invalid daemon arguments in %s", path)
        return []
    return value


def daemon_command(python_exe: Path, args: list[str]) -> list[str]:
    return [str(python_exe), str(DECKD_SCRIPT), *args]


class DeckdController:
    def __init__(
        self,
        python_exe: Path,
        args: list[str],
        log_path: Path = LOG_PATH,
        popen: Callable = subprocess.Popen,
    ) -> None:
        self.python_exe = python_exe
        self.args = args
        self.log_path = log_path
        self._popen = popen
        self.env = os.environ.copy()
        site_packages = str(Path(sys.prefix) / "Lib" / "site-packages")
        existing_pythonpath = self.env.get("PYTHONPATH")
        self.env["PYTHONPATH"] = os.pathsep.join(
            part for part in (site_packages, existing_pythonpath) if part
        )
        self.env["PYTHONNOUSERSITE"] = "1"
        self.process: subprocess.Popen | None = None
        self.icon = None
        self._last_running: bool | None = None
        self._monitor_stop = threading.Event()

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _refresh_icon(self) -> None:
        running = self.running
        if self.icon is not None and running != self._last_running:
            self.icon.icon = make_icon(self.running)
            self.icon.title = (
                "StreamDeck host running" if self.running
                else "StreamDeck host stopped"
            )
            self.icon.update_menu()
            self._last_running = running

    def monitor(self) -> None:
        while not self._monitor_stop.wait(1):
            self._refresh_icon()

    def start(self) -> None:
        if self.running:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as output:
            output.write(f"\n--- Starting deckd at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            output.flush()
            self.process = self._popen(
                daemon_command(self.python_exe, self.args),
                cwd=HOST_DIR,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=self.env,
            )
        self._refresh_icon()

    def stop(self) -> None:
        if not self.running:
            self._refresh_icon()
            return
        process = self.process
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        self._refresh_icon()

    def restart(self) -> None:
        self.stop()
        self.start()

    def quit(self, _icon=None, _item=None) -> None:
        self._monitor_stop.set()
        self.stop()
        if self.icon is not None:
            self.icon.stop()

    def _invoke(self, action: Callable[[], None]) -> None:
        try:
            action()
        except Exception:
            log.exception("Tray action failed")
            if self.icon is not None:
                self.icon.notify("See the StreamDeck log for details", "StreamDeck error")
            self._refresh_icon()

    def open_log(self, _icon=None, _item=None) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.touch(exist_ok=True)
        os.startfile(str(self.log_path))

    def open_gui(self, _icon=None, _item=None) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        gui_python = self.python_exe.with_name("pythonw.exe")
        with GUI_LOG_PATH.open("a", encoding="utf-8") as output:
            output.write(f"\n--- Starting deckgui at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            output.flush()
            self._popen(
                [str(gui_python), str(GUI_SCRIPT)],
                cwd=HOST_DIR,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=self.env,
            )

    def menu(self, pystray):
        return pystray.Menu(
            pystray.MenuItem(
                lambda _item: "Status: running" if self.running else "Status: stopped",
                None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start", lambda icon, item: self._invoke(self.start),
                             enabled=lambda _item: not self.running),
            pystray.MenuItem("Stop", lambda icon, item: self._invoke(self.stop),
                             enabled=lambda _item: self.running),
            pystray.MenuItem("Restart", lambda icon, item: self._invoke(self.restart)),
            pystray.MenuItem("Open GUI", lambda icon, item: self._invoke(self.open_gui)),
            pystray.MenuItem("Open log", lambda icon, item: self._invoke(self.open_log)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit (stops StreamDeck)", self.quit),
        )


def make_icon(running: bool):
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, 60, 60), radius=12, fill="#20232a")
    for row in range(2):
        for column in range(3):
            x = 18 + column * 14
            y = 20 + row * 14
            draw.rounded_rectangle((x - 3, y - 3, x + 3, y + 3), radius=2,
                                   fill="#e8eaed")
    draw.ellipse((41, 41, 57, 57), fill="#27ae60" if running else "#70757a",
                 outline="#20232a", width=2)
    return image


def acquire_single_instance_mutex():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None
    return kernel32, handle


def main() -> int:
    if sys.platform != "win32":
        print("The Windows tray controller can only run on Windows.", file=sys.stderr)
        return 1

    APP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        mutex = acquire_single_instance_mutex()
        if mutex is None:
            log.info("Tray controller is already running")
            return 0
        import pystray

        args = sys.argv[1:] if len(sys.argv) > 1 else load_deckd_args()
        python_exe = Path(getattr(sys, "_base_executable", sys.executable))
        controller = DeckdController(python_exe, args)
        icon = pystray.Icon(
            "StreamDeck",
            make_icon(False),
            "StreamDeck host stopped",
            menu=controller.menu(pystray),
        )
        controller.icon = icon
        controller.start()
        threading.Thread(target=controller.monitor, daemon=True).start()
        try:
            icon.run()
        finally:
            controller._monitor_stop.set()
            controller.stop()
            mutex[0].CloseHandle(mutex[1])
        return 0
    except Exception:
        log.exception("Windows tray controller failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
