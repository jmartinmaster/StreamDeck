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
"""Windows platform backend for deckd (USB Serial + Wi-Fi bare-minimum support).

Provides pure-stdlib ctypes implementations of:
- ``WinKeyInjector``: keyboard shortcut injection via Win32 ``SendInput``.
- ``WinFocusWatcher``: active foreground window tracking via Win32 User32/Kernel32.
- ``find_default_serial_port``: auto-detection of ESP32 USB-UART / USB-JTAG COM ports.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger("deckd")

# ---------------------------------------------------------------------------
# Win32 Virtual-Key mapping (matches deckd.py's MODIFIER_KEYS & KEY_ALIASES)
# ---------------------------------------------------------------------------

VK_MODIFIERS: dict[str, int] = {
    "ctrl": 0xA2,    # VK_LCONTROL
    "lctrl": 0xA2,   # VK_LCONTROL
    "rctrl": 0xA3,   # VK_RCONTROL
    "shift": 0xA0,   # VK_LSHIFT
    "lshift": 0xA0,  # VK_LSHIFT
    "rshift": 0xA1,  # VK_RSHIFT
    "alt": 0xA4,     # VK_LMENU
    "lalt": 0xA4,    # VK_LMENU
    "ralt": 0xA5,    # VK_RMENU
    "super": 0x5B,   # VK_LWIN
    "lsuper": 0x5B,  # VK_LWIN
    "rsuper": 0x5C,  # VK_RWIN
    "meta": 0x5B,    # VK_LWIN
    "win": 0x5B,     # VK_LWIN
    "lwin": 0x5B,    # VK_LWIN
    "rwin": 0x5C,    # VK_RWIN
}

VK_ALIASES: dict[str, int] = {
    "enter": 0x0D,        # VK_RETURN
    "return": 0x0D,       # VK_RETURN
    "esc": 0x1B,          # VK_ESCAPE
    "escape": 0x1B,       # VK_ESCAPE
    "tab": 0x09,          # VK_TAB
    "space": 0x20,        # VK_SPACE
    "backspace": 0x08,    # VK_BACK
    "delete": 0x2E,       # VK_DELETE
    "del": 0x2E,          # VK_DELETE
    "insert": 0x2D,       # VK_INSERT
    "ins": 0x2D,          # VK_INSERT
    "up": 0x26,           # VK_UP
    "down": 0x28,         # VK_DOWN
    "left": 0x25,         # VK_LEFT
    "right": 0x27,        # VK_RIGHT
    "home": 0x24,         # VK_HOME
    "end": 0x23,          # VK_END
    "pageup": 0x21,       # VK_PRIOR
    "pgup": 0x21,         # VK_PRIOR
    "pagedown": 0x22,     # VK_NEXT
    "pgdn": 0x22,         # VK_NEXT
    "capslock": 0x14,     # VK_CAPITAL
    "caps": 0x14,         # VK_CAPITAL
    "numlock": 0x90,      # VK_NUMLOCK
    "scrolllock": 0x91,   # VK_SCROLL
    "pause": 0x13,        # VK_PAUSE
    "break": 0x13,        # VK_PAUSE
    "menu": 0x5D,         # VK_APPS
    "apps": 0x5D,         # VK_APPS
    "volup": 0xAF,        # VK_VOLUME_UP
    "volumeup": 0xAF,     # VK_VOLUME_UP
    "voldown": 0xAE,      # VK_VOLUME_DOWN
    "volumedown": 0xAE,   # VK_VOLUME_DOWN
    "mute": 0xAD,         # VK_VOLUME_MUTE
    "play": 0xB3,         # VK_MEDIA_PLAY_PAUSE
    "playpause": 0xB3,    # VK_MEDIA_PLAY_PAUSE
    "stop": 0xB2,         # VK_MEDIA_STOP
    "next": 0xB0,         # VK_MEDIA_NEXT_TRACK
    "nextsong": 0xB0,     # VK_MEDIA_NEXT_TRACK
    "prev": 0xB1,         # VK_MEDIA_PREV_TRACK
    "previoussong": 0xB1, # VK_MEDIA_PREV_TRACK
    "print": 0x2C,        # VK_SNAPSHOT
    "printscreen": 0x2C,  # VK_SNAPSHOT
    "prtsc": 0x2C,        # VK_SNAPSHOT
    "prtscr": 0x2C,       # VK_SNAPSHOT
    "sysrq": 0x2C,        # VK_SNAPSHOT
    "minus": 0xBD,        # VK_OEM_MINUS
    "-": 0xBD,            # VK_OEM_MINUS
    "equal": 0xBB,        # VK_OEM_PLUS
    "=": 0xBB,            # VK_OEM_PLUS
    "plus": 0x6B,         # VK_ADD
    "comma": 0xBC,        # VK_OEM_COMMA
    ",": 0xBC,            # VK_OEM_COMMA
    "period": 0xBE,       # VK_OEM_PERIOD
    "dot": 0xBE,          # VK_OEM_PERIOD
    ".": 0xBE,            # VK_OEM_PERIOD
    "slash": 0xBF,        # VK_OEM_2
    "/": 0xBF,            # VK_OEM_2
    "backslash": 0xDC,    # VK_OEM_5
    "\\": 0xDC,           # VK_OEM_5
    "semicolon": 0xBA,    # VK_OEM_1
    ";": 0xBA,            # VK_OEM_1
    "apostrophe": 0xDE,   # VK_OEM_7
    "quote": 0xDE,        # VK_OEM_7
    "'": 0xDE,            # VK_OEM_7
    "leftbrace": 0xDB,    # VK_OEM_4
    "lbracket": 0xDB,     # VK_OEM_4
    "[": 0xDB,            # VK_OEM_4
    "rightbrace": 0xDD,   # VK_OEM_6
    "rbracket": 0xDD,     # VK_OEM_6
    "]": 0xDD,            # VK_OEM_6
    "grave": 0xC0,        # VK_OEM_3
    "backtick": 0xC0,     # VK_OEM_3
    "tilde": 0xC0,        # VK_OEM_3
    "`": 0xC0,            # VK_OEM_3
    "~": 0xC0,            # VK_OEM_3
    "kp0": 0x60,          # VK_NUMPAD0
    "kp1": 0x61,          # VK_NUMPAD1
    "kp2": 0x62,          # VK_NUMPAD2
    "kp3": 0x63,          # VK_NUMPAD3
    "kp4": 0x64,          # VK_NUMPAD4
    "kp5": 0x65,          # VK_NUMPAD5
    "kp6": 0x66,          # VK_NUMPAD6
    "kp7": 0x67,          # VK_NUMPAD7
    "kp8": 0x68,          # VK_NUMPAD8
    "kp9": 0x69,          # VK_NUMPAD9
    "numpad0": 0x60,
    "numpad1": 0x61,
    "numpad2": 0x62,
    "numpad3": 0x63,
    "numpad4": 0x64,
    "numpad5": 0x65,
    "numpad6": 0x66,
    "numpad7": 0x67,
    "numpad8": 0x68,
    "numpad9": 0x69,
    "kpplus": 0x6B,       # VK_ADD
    "kpminus": 0x6D,      # VK_SUBTRACT
    "kpasterisk": 0x6A,   # VK_MULTIPLY
    "kpslash": 0x6F,      # VK_DIVIDE
    "kpdot": 0x6E,        # VK_DECIMAL
    "kpenter": 0x0D,      # VK_RETURN (extended)
    "kpequal": 0xBB,      # VK_OEM_PLUS
}

# Virtual keys that require KEYEVENTF_EXTENDEDKEY on Windows.
EXTENDED_VKS: frozenset[int] = frozenset({
    0xA3,  # VK_RCONTROL
    0xA5,  # VK_RMENU
    0x5B,  # VK_LWIN
    0x5C,  # VK_RWIN
    0x5D,  # VK_APPS
    0x21,  # VK_PRIOR (Page Up)
    0x22,  # VK_NEXT (Page Down)
    0x23,  # VK_END
    0x24,  # VK_HOME
    0x25,  # VK_LEFT
    0x26,  # VK_UP
    0x27,  # VK_RIGHT
    0x28,  # VK_DOWN
    0x2C,  # VK_SNAPSHOT (Print Screen)
    0x2D,  # VK_INSERT
    0x2E,  # VK_DELETE
    0x6F,  # VK_DIVIDE
    0x90,  # VK_NUMLOCK
    0xAD,  # VK_VOLUME_MUTE
    0xAE,  # VK_VOLUME_DOWN
    0xAF,  # VK_VOLUME_UP
    0xB0,  # VK_MEDIA_NEXT_TRACK
    0xB1,  # VK_MEDIA_PREV_TRACK
    0xB2,  # VK_MEDIA_STOP
    0xB3,  # VK_MEDIA_PLAY_PAUSE
})


def key_token_to_vk(token: str) -> tuple[int, bool]:
    """Map a deckd key token to ``(vk_code, is_extended)``."""
    raw = token.strip().lower()
    if not raw:
        raise ValueError("empty key token")
    if raw in VK_MODIFIERS:
        vk = VK_MODIFIERS[raw]
        return vk, vk in EXTENDED_VKS
    if raw in VK_ALIASES:
        vk = VK_ALIASES[raw]
        is_ext = (vk in EXTENDED_VKS) or (raw == "kpenter")
        return vk, is_ext
    if len(raw) == 1 and ("a" <= raw <= "z" or "0" <= raw <= "9"):
        return ord(raw.upper()), False
    if raw.startswith("f") and raw[1:].isdigit():
        num = int(raw[1:])
        if 1 <= num <= 24:
            return 0x70 + (num - 1), False  # VK_F1..VK_F24
    raise ValueError(f"unknown key token: {token}")


# ---------------------------------------------------------------------------
# Win32 SendInput structures (x86 and x64 ABI-accurate)
# ---------------------------------------------------------------------------

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
MAPVK_VK_TO_VSC = 0

_ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_int32),
        ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_uint16),
        ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_uint32),
        ("wParamL", ctypes.c_uint16),
        ("wParamH", ctypes.c_uint16),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("union", _INPUT_UNION),
    ]


def _make_key_input(vk: int, scan: int, *, extended: bool = False, keyup: bool = False) -> _INPUT:
    flags = 0
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    if keyup:
        flags |= KEYEVENTF_KEYUP
    return _INPUT(
        type=INPUT_KEYBOARD,
        union=_INPUT_UNION(
            ki=_KEYBDINPUT(
                wVk=vk,
                wScan=scan,
                dwFlags=flags,
                time=0,
                dwExtraInfo=0,
            )
        ),
    )


class WinKeyInjector:
    """Virtual keyboard backed by Win32 ``SendInput``."""

    def __init__(self, user32=None) -> None:
        self._lock = threading.Lock()
        if user32 is not None:
            self._user32 = user32
        elif sys.platform == "win32":
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._user32.SendInput.argtypes = (
                ctypes.c_uint,
                ctypes.POINTER(_INPUT),
                ctypes.c_int,
            )
            self._user32.SendInput.restype = ctypes.c_uint
            self._user32.MapVirtualKeyW.argtypes = (ctypes.c_uint, ctypes.c_uint)
            self._user32.MapVirtualKeyW.restype = ctypes.c_uint
            log.info("Win32 SendInput keyboard injector ready")
        else:
            self._user32 = None

    @property
    def available(self) -> bool:
        return self._user32 is not None

    def _send_inputs(self, events: list[_INPUT]) -> None:
        if not events:
            return
        if self._user32 is None:
            raise RuntimeError("Win32 SendInput unavailable on this platform")
        arr = (_INPUT * len(events))(*events)
        sent = self._user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))
        if sent != len(events):
            err = ctypes.get_last_error() if sys.platform == "win32" else 0
            raise OSError(f"SendInput sent {sent}/{len(events)} events (winerror={err})")

    def send_chord(self, chord: str, hold_s: float = 0.09) -> None:
        if self._user32 is None:
            raise RuntimeError("Win32 SendInput unavailable")
        steps = [
            [key_token_to_vk(token) for token in step.split("+") if token.strip()]
            for step in chord.split(",")
            if step.strip()
        ]
        if not steps or any(not mapped for mapped in steps):
            raise ValueError(f"empty key chord: {chord!r}")
        hold = max(0.0, min(float(hold_s), 5.0))

        with self._lock:
            for idx, mapped in enumerate(steps):
                if idx > 0 and hold > 0:
                    time.sleep(0.04)
                down_events: list[_INPUT] = []
                up_events: list[_INPUT] = []
                for vk, is_ext in mapped:
                    scan = int(self._user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)) & 0xFFFF
                    down_events.append(_make_key_input(vk, scan, extended=is_ext, keyup=False))
                    up_events.append(_make_key_input(vk, scan, extended=is_ext, keyup=True))
                up_events.reverse()
                self._send_inputs(down_events)
                if hold > 0:
                    time.sleep(hold)
                self._send_inputs(up_events)

    def unlock_sequence(self, password_bytes: bytearray, delay_ms: int = 1000) -> None:
        for i in range(len(password_bytes)):
            password_bytes[i] = 0
        raise RuntimeError(
            "remote lock-screen unlock is not supported on Windows "
            "(Secure Desktop blocks SendInput)"
        )

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Win32 foreground window focus tracker
# ---------------------------------------------------------------------------

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _query_foreground_window_win32() -> tuple[str, str] | None:
    """Return ``(exe_stem_lower, window_title)`` for the current foreground window."""
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None

    # Window title
    title_len = user32.GetWindowTextLengthW(hwnd)
    if title_len > 0:
        buf = ctypes.create_unicode_buffer(title_len + 1)
        user32.GetWindowTextW(hwnd, buf, len(buf))
        title = buf.value
    else:
        title = ""

    # Owning process executable name
    pid = ctypes.c_uint32(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    app_id = ""
    if pid.value:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if handle:
            try:
                path_len = ctypes.c_uint32(1024)
                path_buf = ctypes.create_unicode_buffer(path_len.value)
                if kernel32.QueryFullProcessImageNameW(
                    handle, 0, path_buf, ctypes.byref(path_len)
                ):
                    app_id = Path(path_buf.value).stem.lower()
            finally:
                kernel32.CloseHandle(handle)

    if not app_id and not title:
        return None
    return app_id, title


class WinFocusWatcher:
    """Polls the active Windows foreground window in a background thread.

    Exposes the same interface as Linux's ``FocusWatcher`` in ``deckd.py``:
    ``start()``, ``push(app_id, title)``, ``current()``, and ``activate_app()``.
    """

    def __init__(
        self,
        poll_interval: float = 0.15,
        query_fn: Callable[[], tuple[str, str] | None] | None = None,
    ) -> None:
        self.poll_interval = poll_interval
        self._query_fn = query_fn or _query_foreground_window_win32
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._latest: tuple[str, str] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="win-focus-watcher", daemon=True
        )
        self._thread.start()
        log.info("Windows foreground focus watcher started")

    def stop(self) -> None:
        self._stop.set()

    def push(self, app_id: str, title: str) -> None:
        self._queue.put((app_id, title))

    def current(self) -> tuple[str, str] | None:
        while True:
            try:
                self._latest = self._queue.get_nowait()
            except queue.Empty:
                break
        return self._latest

    def activate_app(self, app_id: str, timeout: float = 1.0) -> bool:
        return False

    def _run(self) -> None:
        last_sent: tuple[str, str] | None = None
        while not self._stop.is_set():
            try:
                state = self._query_fn()
                if state is not None and state != last_sent:
                    last_sent = state
                    self.push(state[0], state[1])
            except Exception as error:
                log.debug("Windows focus poll error: %s", error)
            self._stop.wait(self.poll_interval)


# ---------------------------------------------------------------------------
# ESP32 COM port auto-detection
# ---------------------------------------------------------------------------

# Common USB VIDs for ESP32 / ESP32-S3 boards and USB-UART bridges:
# 0x303A = Espressif native USB JTAG/Serial
# 0x10C4 = Silicon Labs CP210x
# 0x1A86 = WCH CH340/CH341/CH9102
# 0x0403 = FTDI FT232
ESP32_USB_VIDS: frozenset[int] = frozenset({0x303A, 0x10C4, 0x1A86, 0x0403})
_ESP32_DESC_KEYWORDS = ("esp32", "cp210", "ch340", "ch910", "usb serial", "uart", "jtag")


def find_default_serial_port(ports: Iterable | None = None) -> str | None:
    """Return the most likely ESP32 COM/serial port, or ``None`` if not found."""
    if ports is None:
        try:
            from serial.tools import list_ports

            port_list = list(list_ports.comports())
        except Exception as error:
            log.debug("serial port enumeration failed: %s", error)
            return None
    else:
        port_list = list(ports)

    if not port_list:
        return None

    # 1. Known ESP32 / USB-UART bridge VIDs
    for info in port_list:
        if getattr(info, "vid", None) in ESP32_USB_VIDS:
            return str(info.device)

    # 2. Description / manufacturer keyword match
    for info in port_list:
        hay = f"{getattr(info, 'description', '')} {getattr(info, 'manufacturer', '')}".lower()
        if any(kw in hay for kw in _ESP32_DESC_KEYWORDS):
            return str(info.device)

    # 3. Single available port fallback
    if len(port_list) == 1:
        return str(port_list[0].device)

    return None
