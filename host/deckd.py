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
"""deckd - local Stream Deck daemon.

Connects to the deck over USB serial, WiFi TCP (advertised via mDNS as
``_mmdeckd._tcp``), or Bluetooth LE (NUS GATT), pushes button layouts and
launcher apps from profiles.toml, and executes shortcuts, shell commands, or
XDG desktop applications when the device reports a button press.
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import logging
import math
import queue
import select
import socket
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import serial

try:
    from desktop_apps import (
        DesktopApp,
        discover_desktop_apps,
        guess_deck_icon,
        launch_desktop_app,
        normalize_desktop_id,
        short_label,
        suggest_smart_defaults,
    )
except ImportError:  # pragma: no cover - package import fallback
    from .desktop_apps import (  # type: ignore[no-redef]
        DesktopApp,
        discover_desktop_apps,
        guess_deck_icon,
        launch_desktop_app,
        normalize_desktop_id,
        short_label,
        suggest_smart_defaults,
    )

try:
    from evdev import UInput, ecodes

    HAVE_EVDEV = True
except ImportError:  # pragma: no cover - optional dependency
    HAVE_EVDEV = False

try:
    from dbus_next.aio import MessageBus

    HAVE_DBUS = True
except ImportError:  # pragma: no cover - optional dependency
    HAVE_DBUS = False

try:
    from deckcontrol import DeckControl
except ImportError:  # pragma: no cover - running outside tools/deckd
    from .deckcontrol import DeckControl  # type: ignore[no-redef]

try:
    from unlock_crypto import UnlockKeyStore

    HAVE_UNLOCK_CRYPTO = True
except ImportError:  # pragma: no cover - optional dependency fallback
    try:
        from .unlock_crypto import UnlockKeyStore  # type: ignore[no-redef]

        HAVE_UNLOCK_CRYPTO = True
    except ImportError:
        UnlockKeyStore = None  # type: ignore[assignment,misc]
        HAVE_UNLOCK_CRYPTO = False

try:
    from zeroconf import ServiceInfo, Zeroconf

    HAVE_ZEROCONF = True
except ImportError:  # pragma: no cover - optional dependency
    HAVE_ZEROCONF = False

try:
    from ble_deck import BleListener

    HAVE_BLE = True
except ImportError:  # pragma: no cover - optional dependency
    try:
        from .ble_deck import BleListener  # type: ignore[no-redef]

        HAVE_BLE = True
    except ImportError:
        HAVE_BLE = False

log = logging.getLogger("deckd")

PAGE_SIZE = 16
MAX_PROFILE_ID_BYTES = 63
MAX_PROFILE_CATALOG_BYTES = 4096
HEARTBEAT_INTERVAL = 5.0
RECONNECT_DELAY = 2.0
ACTIVATE_SETTLE = 0.25  # seconds to let GNOME finish raising a lock_target app
DEFAULT_KEY_HOLD_S = 0.090  # ~90 ms standard key press duration for desktop & game compatibility
CONTAINER_APP_IDS = frozenset({
    "steam_steam",
    "steam",
    "steamwebhelper",
    "wine",
    "wine-preloader",
    "wine64-preloader",
    "bottles",
})

TCP_PORT = 4650
TCP_READ_TIMEOUT = 0.2
TCP_IDLE_TIMEOUT = 15.0  # drop a TCP client after this long without RX
BLE_IDLE_TIMEOUT = 15.0  # drop a BLE client after this long without RX
SERIAL_IDLE_TIMEOUT = 3.0  # yield silent serial port to BLE/TCP after 3s
MDNS_SERVICE_TYPE = "_mmdeckd._tcp.local."

FOCUS_BUS_NAME = "org.mmesp32.DeckFocus"
FOCUS_OBJECT_PATH = "/org/mmesp32/DeckFocus"
FOCUS_INTERFACE = "org.mmesp32.DeckFocus"
FOCUS_RETRY_INTERVAL = 10.0

MODIFIER_KEYS = {
    "ctrl": "KEY_LEFTCTRL",
    "lctrl": "KEY_LEFTCTRL",
    "rctrl": "KEY_RIGHTCTRL",
    "shift": "KEY_LEFTSHIFT",
    "lshift": "KEY_LEFTSHIFT",
    "rshift": "KEY_RIGHTSHIFT",
    "alt": "KEY_LEFTALT",
    "lalt": "KEY_LEFTALT",
    "ralt": "KEY_RIGHTALT",
    "super": "KEY_LEFTMETA",
    "lsuper": "KEY_LEFTMETA",
    "rsuper": "KEY_RIGHTMETA",
    "meta": "KEY_LEFTMETA",
}

KEY_ALIASES = {
    "enter": "KEY_ENTER",
    "return": "KEY_ENTER",
    "esc": "KEY_ESC",
    "escape": "KEY_ESC",
    "tab": "KEY_TAB",
    "space": "KEY_SPACE",
    "backspace": "KEY_BACKSPACE",
    "delete": "KEY_DELETE",
    "del": "KEY_DELETE",
    "insert": "KEY_INSERT",
    "ins": "KEY_INSERT",
    "up": "KEY_UP",
    "down": "KEY_DOWN",
    "left": "KEY_LEFT",
    "right": "KEY_RIGHT",
    "home": "KEY_HOME",
    "end": "KEY_END",
    "pageup": "KEY_PAGEUP",
    "pgup": "KEY_PAGEUP",
    "pagedown": "KEY_PAGEDOWN",
    "pgdn": "KEY_PAGEDOWN",
    "capslock": "KEY_CAPSLOCK",
    "caps": "KEY_CAPSLOCK",
    "numlock": "KEY_NUMLOCK",
    "scrolllock": "KEY_SCROLLLOCK",
    "pause": "KEY_PAUSE",
    "break": "KEY_PAUSE",
    "menu": "KEY_COMPOSE",
    "apps": "KEY_COMPOSE",
    "volup": "KEY_VOLUMEUP",
    "voldown": "KEY_VOLUMEDOWN",
    "mute": "KEY_MUTE",
    "play": "KEY_PLAYPAUSE",
    "stop": "KEY_STOPCD",
    "next": "KEY_NEXTSONG",
    "prev": "KEY_PREVIOUSSONG",
    "print": "KEY_SYSRQ",
    "printscreen": "KEY_SYSRQ",
    "prtsc": "KEY_SYSRQ",
    "prtscr": "KEY_SYSRQ",
    "sysrq": "KEY_SYSRQ",
    "minus": "KEY_MINUS",
    "-": "KEY_MINUS",
    "equal": "KEY_EQUAL",
    "plus": "KEY_KPPLUS",
    "=": "KEY_EQUAL",
    "comma": "KEY_COMMA",
    "period": "KEY_DOT",
    "dot": "KEY_DOT",
    ".": "KEY_DOT",
    "slash": "KEY_SLASH",
    "/": "KEY_SLASH",
    "backslash": "KEY_BACKSLASH",
    "\\": "KEY_BACKSLASH",
    "semicolon": "KEY_SEMICOLON",
    ";": "KEY_SEMICOLON",
    "apostrophe": "KEY_APOSTROPHE",
    "quote": "KEY_APOSTROPHE",
    "'": "KEY_APOSTROPHE",
    "leftbrace": "KEY_LEFTBRACE",
    "lbracket": "KEY_LEFTBRACE",
    "[": "KEY_LEFTBRACE",
    "rightbrace": "KEY_RIGHTBRACE",
    "rbracket": "KEY_RIGHTBRACE",
    "]": "KEY_RIGHTBRACE",
    "grave": "KEY_GRAVE",
    "backtick": "KEY_GRAVE",
    "tilde": "KEY_GRAVE",
    "`": "KEY_GRAVE",
    "~": "KEY_GRAVE",
    "kp0": "KEY_KP0",
    "kp1": "KEY_KP1",
    "kp2": "KEY_KP2",
    "kp3": "KEY_KP3",
    "kp4": "KEY_KP4",
    "kp5": "KEY_KP5",
    "kp6": "KEY_KP6",
    "kp7": "KEY_KP7",
    "kp8": "KEY_KP8",
    "kp9": "KEY_KP9",
    "numpad0": "KEY_KP0",
    "numpad1": "KEY_KP1",
    "numpad2": "KEY_KP2",
    "numpad3": "KEY_KP3",
    "numpad4": "KEY_KP4",
    "numpad5": "KEY_KP5",
    "numpad6": "KEY_KP6",
    "numpad7": "KEY_KP7",
    "numpad8": "KEY_KP8",
    "numpad9": "KEY_KP9",
    "kpplus": "KEY_KPPLUS",
    "kpminus": "KEY_KPMINUS",
    "kpasterisk": "KEY_KPASTERISK",
    "kpslash": "KEY_KPSLASH",
    "kpdot": "KEY_KPDOT",
    "kpenter": "KEY_KPENTER",
    "kpequal": "KEY_KPEQUAL",
}


def key_token_to_code(token: str) -> int:
    token = token.strip().lower()
    if token in MODIFIER_KEYS:
        name = MODIFIER_KEYS[token]
    elif token in KEY_ALIASES:
        name = KEY_ALIASES[token]
    elif len(token) == 1 and token.isalnum():
        name = f"KEY_{token.upper()}"
    elif token.startswith("f") and token[1:].isdigit():
        name = f"KEY_{token.upper()}"
    else:
        raise ValueError(f"unknown key token: {token}")
    code = getattr(ecodes, name, None)
    if code is None:
        raise ValueError(f"unmapped key token: {token}")
    return code


_UNSHIFTED_ASCII = {
    " ": "KEY_SPACE",
    "-": "KEY_MINUS",
    "=": "KEY_EQUAL",
    "[": "KEY_LEFTBRACE",
    "]": "KEY_RIGHTBRACE",
    "\\": "KEY_BACKSLASH",
    ";": "KEY_SEMICOLON",
    "'": "KEY_APOSTROPHE",
    "`": "KEY_GRAVE",
    ",": "KEY_COMMA",
    ".": "KEY_DOT",
    "/": "KEY_SLASH",
}

_SHIFTED_ASCII = {
    "!": "KEY_1",
    "@": "KEY_2",
    "#": "KEY_3",
    "$": "KEY_4",
    "%": "KEY_5",
    "^": "KEY_6",
    "&": "KEY_7",
    "*": "KEY_8",
    "(": "KEY_9",
    ")": "KEY_0",
    "_": "KEY_MINUS",
    "+": "KEY_EQUAL",
    "{": "KEY_LEFTBRACE",
    "}": "KEY_RIGHTBRACE",
    "|": "KEY_BACKSLASH",
    ":": "KEY_SEMICOLON",
    '"': "KEY_APOSTROPHE",
    "~": "KEY_GRAVE",
    "<": "KEY_COMMA",
    ">": "KEY_DOT",
    "?": "KEY_SLASH",
}


def char_to_evdev(ch: str) -> tuple[int, bool]:
    """Map one printable US-QWERTY ASCII character to (evdev keycode, shift)."""
    if len(ch) != 1:
        raise ValueError("expected single character")
    if "a" <= ch <= "z":
        name, shift = f"KEY_{ch.upper()}", False
    elif "A" <= ch <= "Z":
        name, shift = f"KEY_{ch}", True
    elif "0" <= ch <= "9":
        name, shift = f"KEY_{ch}", False
    elif ch in _UNSHIFTED_ASCII:
        name, shift = _UNSHIFTED_ASCII[ch], False
    elif ch in _SHIFTED_ASCII:
        name, shift = _SHIFTED_ASCII[ch], True
    else:
        raise ValueError(f"unsupported character code {ord(ch):#x}")
    code = getattr(ecodes, name, None)
    if code is None:
        raise ValueError(f"unmapped key: {name}")
    return code, shift


def _keyboard_uinput_events() -> dict[int, list[int]]:
    """Build a pure keyboard EV_KEY capability list (no mouse/gamepad BTN_* codes)."""
    if not HAVE_EVDEV:
        return {}
    key_codes = sorted({
        code for name, code in ecodes.ecodes.items()
        if name.startswith("KEY_") and isinstance(code, int)
        and 0 < code < ecodes.KEY_CNT
        and name not in ("KEY_MAX", "KEY_CNT", "KEY_MIN_INTERESTING")
    })
    return {ecodes.EV_KEY: key_codes}


class KeyInjector:
    """Virtual keyboard backed by /dev/uinput."""

    def __init__(self) -> None:
        self._uinput = None
        self._lock = threading.Lock()
        self._last_uinput_try = 0.0
        if not HAVE_EVDEV:
            log.warning("python-evdev not installed; 'keys' actions disabled")
            return
        self._open_uinput_locked(initial=True)

    def _open_uinput_locked(self, initial: bool = False) -> bool:
        """Lazily open /dev/uinput if needed so daemon recovers after login ACLs settle."""
        if self._uinput is not None:
            return True
        if not HAVE_EVDEV:
            return False
        now = time.monotonic()
        last_try = getattr(self, "_last_uinput_try", 0.0)
        if not initial and last_try > 0.0 and (now - last_try) < 1.0:
            return False
        if not initial:
            self._last_uinput_try = now
        try:
            events = _keyboard_uinput_events()
            if events:
                self._uinput = UInput(events=events, name="deckd-virtual-keyboard")
            else:
                self._uinput = UInput(name="deckd-virtual-keyboard")
            self._last_uinput_try = 0.0
            log.info("uinput virtual keyboard ready")
            return True
        except Exception as error:  # PermissionError or missing /dev/uinput
            if initial:
                log.warning("uinput unavailable (%s); 'keys' actions disabled", error)
            else:
                log.debug("uinput still unavailable (%s)", error)
            return False

    def _reset_uinput_locked(self) -> None:
        if self._uinput is not None:
            try:
                self._uinput.close()
            except Exception:
                pass
            self._uinput = None

    @property
    def available(self) -> bool:
        if self._uinput is not None:
            return True
        with self._lock:
            return self._open_uinput_locked()

    def send_chord(self, chord: str, hold_s: float = DEFAULT_KEY_HOLD_S) -> None:
        steps = [
            [key_token_to_code(token) for token in step.split("+") if token.strip()]
            for step in chord.split(",")
            if step.strip()
        ]
        if not steps or any(not codes for codes in steps):
            raise ValueError(f"empty key chord: {chord!r}")
        hold = max(0.01, min(float(hold_s), 5.0))
        with self._lock:
            if not self._open_uinput_locked():
                raise RuntimeError("uinput unavailable")
            assert self._uinput is not None
            try:
                for idx, codes in enumerate(steps):
                    if idx > 0:
                        time.sleep(0.04)
                    for code in codes:
                        self._uinput.write(ecodes.EV_KEY, code, 1)
                    self._uinput.syn()
                    time.sleep(hold)
                    for code in reversed(codes):
                        self._uinput.write(ecodes.EV_KEY, code, 0)
                    self._uinput.syn()
            except OSError:
                self._reset_uinput_locked()
                raise

    def _tap_key(self, code: int, hold_s: float = 0.01) -> None:
        assert self._uinput is not None
        self._uinput.write(ecodes.EV_KEY, code, 1)
        self._uinput.syn()
        time.sleep(hold_s)
        self._uinput.write(ecodes.EV_KEY, code, 0)
        self._uinput.syn()

    def unlock_sequence(self, password_bytes: bytearray, delay_ms: int = 1000) -> None:
        """Wake screen, wait delay_ms, type password, and press Enter."""
        try:
            if not password_bytes:
                raise ValueError("empty password")
            text = password_bytes.decode("utf-8")
            key_steps = [char_to_evdev(ch) for ch in text]
            del text
            wait_s = max(0, min(int(delay_ms), 10000)) / 1000.0
            shift_code = ecodes.KEY_LEFTSHIFT
            enter_code = ecodes.KEY_ENTER

            with self._lock:
                if not self._open_uinput_locked():
                    raise RuntimeError("uinput unavailable")
                try:
                    self._tap_key(shift_code, hold_s=0.02)
                    if wait_s > 0:
                        time.sleep(wait_s)
                    for code, shift in key_steps:
                        if shift:
                            self._uinput.write(ecodes.EV_KEY, shift_code, 1)
                            self._uinput.syn()
                        self._uinput.write(ecodes.EV_KEY, code, 1)
                        self._uinput.syn()
                        time.sleep(0.004)
                        self._uinput.write(ecodes.EV_KEY, code, 0)
                        self._uinput.syn()
                        if shift:
                            self._uinput.write(ecodes.EV_KEY, shift_code, 0)
                            self._uinput.syn()
                        time.sleep(0.004)
                    time.sleep(0.02)
                    self._tap_key(enter_code, hold_s=0.015)
                except OSError:
                    self._reset_uinput_locked()
                    raise
        finally:
            for i in range(len(password_bytes)):
                password_bytes[i] = 0


MAX_LAUNCHER_APPS = 72


@dataclass
class Button:
    label: str
    keys: str | None = None
    shell: str | None = None
    app: str | None = None
    icon: str | None = None
    color: str | None = None
    hold_ms: int | None = None


@dataclass
class LauncherConfig:
    apps: list[Button] = field(default_factory=list)
    auto_return: bool = True


@dataclass
class Group:
    title: str
    buttons: list[Button] = field(default_factory=list)
    color: str | None = None


@dataclass
class Profile:
    app_id: str
    buttons: list[Button] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    lock_target: bool = False
    aliases: list[str] = field(default_factory=list)
    match_title: list[str] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        if self.groups:
            return max(1, math.ceil(len(self.groups) / 2))
        return max(1, math.ceil(len(self.buttons) / PAGE_SIZE))

    def page_groups(self, index: int) -> tuple[str, str]:
        if not self.groups:
            return ("", "")
        idx = (index % self.page_count) * 2
        g1 = self.groups[idx].title if idx < len(self.groups) else ""
        g2 = self.groups[idx + 1].title if idx + 1 < len(self.groups) else ""
        return (g1, g2)

    def page(self, index: int) -> list[Button]:
        if self.groups:
            idx = (index % self.page_count) * 2
            g1_btns = [Button(**b.__dict__) for b in self.groups[idx].buttons[:8]] if idx < len(self.groups) else []
            g1_btns += [Button(label="")] * (8 - len(g1_btns))
            g2_btns = [Button(**b.__dict__) for b in self.groups[idx + 1].buttons[:8]] if idx + 1 < len(self.groups) else []
            g2_btns += [Button(label="")] * (8 - len(g2_btns))
            return g1_btns + g2_btns
        start = (index % self.page_count) * PAGE_SIZE
        return self.buttons[start : start + PAGE_SIZE]


def _button_from_entry(entry: dict, default_color: str | None = None) -> Button:
    raw_hold = entry.get("hold_ms")
    hold_ms = int(raw_hold) if raw_hold is not None else None
    return Button(
        label=str(entry.get("label", "?"))[:13],
        keys=entry.get("keys"),
        shell=entry.get("shell"),
        app=entry.get("app"),
        icon=entry.get("icon"),
        color=entry.get("color") or default_color,
        hold_ms=hold_ms,
    )


def _resolve_launcher_config(raw: dict) -> LauncherConfig:
    section = raw.get("launcher")
    discovered: list[DesktopApp] | None = None
    by_id: dict[str, DesktopApp] | None = None

    def get_apps() -> tuple[list[DesktopApp], dict[str, DesktopApp]]:
        nonlocal discovered, by_id
        if discovered is None:
            discovered = discover_desktop_apps()
            by_id = {a.desktop_id: a for a in discovered}
        assert by_id is not None
        return discovered, by_id

    if not isinstance(section, dict) or ("apps" not in section and not section.get("configured")):
        auto_ret = True if not isinstance(section, dict) else bool(section.get("auto_return", True))
        apps, _ = get_apps()
        defaults = suggest_smart_defaults(apps)
        buttons = [
            Button(
                label=entry["label"][:13],
                app=entry["id"],
                icon=entry.get("icon"),
                color=entry.get("color", "accent"),
            )
            for entry in defaults[:MAX_LAUNCHER_APPS]
        ]
        return LauncherConfig(apps=buttons, auto_return=auto_ret)

    auto_ret = bool(section.get("auto_return", True))
    raw_apps = section.get("apps", [])
    buttons: list[Button] = []
    if raw_apps:
        _, app_map = get_apps()
        for entry in raw_apps[:MAX_LAUNCHER_APPS]:
            if isinstance(entry, str):
                entry = {"id": entry}
            desktop_id = normalize_desktop_id(str(entry.get("id") or entry.get("app") or ""))
            meta = app_map.get(desktop_id)
            raw_label = str(entry.get("label", "")).strip()
            if not raw_label:
                raw_label = short_label(meta.name) if meta else desktop_id[:-8]
            icon = entry.get("icon") or (guess_deck_icon(meta) if meta else "app")
            color = entry.get("color") or "accent"
            buttons.append(Button(
                label=raw_label[:13],
                app=desktop_id or None,
                shell=entry.get("shell"),
                keys=entry.get("keys"),
                icon=icon,
                color=color,
            ))
    return LauncherConfig(apps=buttons, auto_return=auto_ret)


def load_profiles_and_launcher(path: Path) -> tuple[dict[str, Profile], LauncherConfig]:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    profiles: dict[str, Profile] = {}
    for app_id, body in raw.get("app", {}).items():
        groups: list[Group] = []
        for g_entry in body.get("groups", []):
            g_buttons = [
                _button_from_entry(entry, g_entry.get("color"))
                for entry in g_entry.get("buttons", [])
            ]
            groups.append(Group(
                title=str(g_entry.get("title", ""))[:31],
                buttons=g_buttons,
                color=g_entry.get("color"),
            ))

        buttons = [_button_from_entry(entry) for entry in body.get("buttons", [])]
        if groups and not buttons:
            flat: list[Button] = []
            for g in groups:
                flat.extend(g.buttons)
            buttons = flat

        profiles[app_id] = Profile(
            app_id=app_id,
            buttons=buttons,
            groups=groups,
            lock_target=bool(body.get("lock_target", False)),
            aliases=[str(a) for a in body.get("aliases", []) if str(a).strip()],
            match_title=[str(t) for t in body.get("match_title", []) if str(t).strip()],
        )
    if "default" not in profiles:
        profiles["default"] = Profile(app_id="default")
    launcher = _resolve_launcher_config(raw)
    log.info("loaded %d profile(s) and %d launcher app(s) from %s",
             len(profiles), len(launcher.apps), path)
    return profiles, launcher


def load_profiles(path: Path) -> dict[str, Profile]:
    profiles, _ = load_profiles_and_launcher(path)
    return profiles


PROFILE_RELOAD_INTERVAL = 2.0


class ProfileStore:
    """Holds the shared profiles dict and launcher config, hot-reloading on change."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.profiles, self.launcher = load_profiles_and_launcher(path)
        self._mtime = self._stat()
        self._last_check = 0.0

    def _stat(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    def maybe_reload(self) -> bool:
        now = time.monotonic()
        if now - self._last_check < PROFILE_RELOAD_INTERVAL:
            return False
        self._last_check = now
        mtime = self._stat()
        if mtime is None or mtime == self._mtime:
            return False
        self._mtime = mtime
        try:
            fresh_profiles, fresh_launcher = load_profiles_and_launcher(self.path)
        except Exception as error:
            log.warning("profiles reload failed (%s); keeping previous set", error)
            return False
        self.profiles.clear()
        self.profiles.update(fresh_profiles)
        self.launcher.apps[:] = fresh_launcher.apps
        self.launcher.auto_return = fresh_launcher.auto_return
        log.info("profiles hot-reloaded")
        return True


class FocusWatcher:
    """Watches the GNOME Shell deckfocus extension over the session D-Bus.

    Runs an asyncio MessageBus in a daemon thread and forwards
    (app_id, title) tuples into a thread-safe queue. If the bus name is
    absent (extension not loaded), it logs a warning once and retries every
    ``retry_interval`` seconds; the daemon keeps running unaffected.
    """

    def __init__(self, bus_name: str = FOCUS_BUS_NAME,
                 object_path: str = FOCUS_OBJECT_PATH,
                 interface: str = FOCUS_INTERFACE,
                 retry_interval: float = FOCUS_RETRY_INTERVAL) -> None:
        self.bus_name = bus_name
        self.object_path = object_path
        self.interface = interface
        self.retry_interval = retry_interval
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._latest: tuple[str, str] | None = None
        self._warned = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._iface = None

    # -- thread-safe side (main loop) ----------------------------------------

    def start(self) -> None:
        if not HAVE_DBUS:
            log.warning("dbus-next not installed; focus awareness disabled")
            return
        self._thread = threading.Thread(
            target=self._thread_main, name="focus-watcher", daemon=True)
        self._thread.start()

    def push(self, app_id: str, title: str) -> None:
        self._queue.put((app_id, title))

    def current(self) -> tuple[str, str] | None:
        """Drain the queue and return the most recent focus tuple, if any."""
        while True:
            try:
                self._latest = self._queue.get_nowait()
            except queue.Empty:
                break
        return self._latest

    def activate_app(self, app_id: str, timeout: float = 1.0) -> bool:
        """Ask GNOME Shell to raise/focus app_id. Thread-safe; best-effort."""
        loop, iface = self._loop, self._iface
        if loop is None or iface is None:
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(
                iface.call_activate_app(app_id), loop)
            return bool(future.result(timeout))
        except AttributeError:
            log.warning("focus extension has no ActivateApp "
                        "(outdated copy installed? re-login after updating)")
            return False
        except Exception as error:
            log.warning("ActivateApp(%s) failed: %s", app_id, error)
            return False

    # -- asyncio side (daemon thread) ----------------------------------------

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as error:  # pragma: no cover - never expected
            log.warning("focus watcher stopped: %s", error)

    def _warn_once(self, message: str) -> None:
        if not self._warned:
            log.warning("%s; staying on default profile, retrying every %.0fs",
                        message, self.retry_interval)
            self._warned = True

    async def _run(self) -> None:
        while True:
            try:
                bus = await MessageBus().connect()
            except Exception as error:
                self._warn_once(f"session bus unavailable ({error})")
                await asyncio.sleep(self.retry_interval)
                continue
            try:
                await self._watch(bus)
            except Exception as error:
                log.warning("focus watcher error: %s; reconnecting", error)
            finally:
                self._loop = None
                self._iface = None
                try:
                    bus.disconnect()
                except Exception:
                    pass
            await asyncio.sleep(self.retry_interval)

    async def _watch(self, bus) -> None:
        intro = await bus.introspect("org.freedesktop.DBus",
                                     "/org/freedesktop/DBus")
        dbus_obj = bus.get_proxy_object("org.freedesktop.DBus",
                                        "/org/freedesktop/DBus", intro)
        dbus_iface = dbus_obj.get_interface("org.freedesktop.DBus")

        owner_lost = asyncio.Event()

        def on_owner_changed(name: str, old: str, new: str) -> None:
            if name == self.bus_name and not new:
                owner_lost.set()

        dbus_iface.on_name_owner_changed(on_owner_changed)

        while True:
            if not await dbus_iface.call_name_has_owner(self.bus_name):
                self._warn_once(
                    f"focus service {self.bus_name} not on the bus "
                    "(GNOME extension not loaded?)")
                await asyncio.sleep(self.retry_interval)
                continue

            owner_lost.clear()
            intro = await bus.introspect(self.bus_name, self.object_path)
            obj = bus.get_proxy_object(self.bus_name, self.object_path, intro)
            iface = obj.get_interface(self.interface)

            def on_focus_changed(app_id: str, title: str) -> None:
                self.push(app_id, title)

            iface.on_focus_changed(on_focus_changed)
            self._loop = asyncio.get_running_loop()
            self._iface = iface
            app_id, title = await iface.call_get_focus()
            self.push(app_id, title)
            log.info("focus service connected (%s)", self.bus_name)
            self._warned = False

            await owner_lost.wait()
            self._iface = None
            iface.off_focus_changed(on_focus_changed)
            log.warning("focus service %s vanished; waiting for it to return",
                        self.bus_name)


class TcpTransport:
    """Serial-like adapter over a connected TCP socket.

    Exposes ``read(n)``/``write(bytes)`` with the same semantics DeckSession
    expects from pyserial: ``read`` blocks for at most TCP_READ_TIMEOUT and
    returns b"" when no data arrived; a closed peer raises ConnectionError.
    """

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.sock.settimeout(TCP_READ_TIMEOUT)

    def read(self, size: int) -> bytes:
        try:
            data = self.sock.recv(size)
        except TimeoutError:
            return b""
        if not data:
            raise ConnectionError("peer closed the connection")
        return data

    def write(self, data: bytes) -> None:
        self.sock.sendall(data)

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class TcpListener:
    """Non-blocking TCP listener for device-initiated WiFi connections."""

    def __init__(self, port: int, host: str = "0.0.0.0") -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(2)
        self.sock.setblocking(False)
        self.port: int = self.sock.getsockname()[1]

    def accept(self, timeout: float = 0.0) -> socket.socket | None:
        ready, _, _ = select.select([self.sock], [], [], timeout)
        if not ready:
            return None
        try:
            conn, addr = self.sock.accept()
        except OSError:
            return None
        log.info("TCP client connected from %s:%d", addr[0], addr[1])
        return conn

    def refuse_pending(self) -> None:
        """Politely turn away queued clients while another session is live."""
        while True:
            ready, _, _ = select.select([self.sock], [], [], 0)
            if not ready:
                return
            try:
                conn, addr = self.sock.accept()
            except OSError:
                return
            log.info("refusing TCP client %s:%d (session already active)",
                     addr[0], addr[1])
            try:
                conn.sendall(b"DECK BYE\n")
            except OSError:
                pass
            conn.close()

    def close(self) -> None:
        self.sock.close()


def guess_lan_ip() -> str:
    """Best-effort LAN IP (UDP connect trick; no packets are sent)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


class MdnsAdvertiser:
    """Advertises the TCP listener as _mmdeckd._tcp via zeroconf."""

    def __init__(self, port: int) -> None:
        self._zeroconf = None
        self._info = None
        if not HAVE_ZEROCONF:
            log.warning("zeroconf not installed; mDNS advertisement disabled")
            return
        ip = guess_lan_ip()
        host = socket.gethostname().split(".")[0]
        self._info = ServiceInfo(
            MDNS_SERVICE_TYPE,
            f"deckd-{host}.{MDNS_SERVICE_TYPE}",
            addresses=[socket.inet_aton(ip)],
            port=port,
            properties={"proto": "1"},
            server=f"{host}.local.",
        )
        try:
            self._zeroconf = Zeroconf()
            self._zeroconf.register_service(self._info)
            log.info("advertising %s at %s:%d", MDNS_SERVICE_TYPE, ip, port)
        except Exception as error:
            log.warning("mDNS advertisement failed (%s); device must use a "
                        "cached address", error)
            self._zeroconf = None

    def close(self) -> None:
        if self._zeroconf is not None:
            try:
                self._zeroconf.unregister_service(self._info)
                self._zeroconf.close()
            except Exception:
                pass
            self._zeroconf = None


class DeckSession:
    """One session with the device over a serial-like transport.

    ``port`` only needs pyserial-style ``read(n)``/``write(bytes)`` with a
    short read timeout; both ``serial.Serial`` and ``TcpTransport`` qualify.
    """

    def __init__(self, port, profiles: dict[str, Profile],
                 injector: KeyInjector,
                 focus_watcher: FocusWatcher | None = None,
                 profile_store: ProfileStore | None = None,
                 control: DeckControl | None = None,
                 transport_name: str = "usb",
                 launcher: LauncherConfig | None = None,
                 unlock_store: "UnlockKeyStore | None" = None) -> None:
        self.port = port
        self.profiles = profiles
        self.injector = injector
        self.focus_watcher = focus_watcher
        self.profile_store = profile_store
        self.launcher = launcher if launcher is not None else (
            profile_store.launcher if profile_store is not None else LauncherConfig()
        )
        self.control = control
        self.transport_name = transport_name
        self.unlock_store = unlock_store
        self._applied_override = ""
        self._device_override = ""
        self.active_app = "default"
        self.focus_app_id: str | None = None
        self.focus_title: str | None = None
        self.page_index = 0
        self.last_ping = 0.0
        self.last_rx = time.monotonic()
        self._rx_buffer = b""
        self._unlock_results: queue.SimpleQueue[tuple[str, bool, str]] = queue.SimpleQueue()
        self._unlock_busy = threading.Event()

    # -- transport -----------------------------------------------------------

    def send(self, line: str) -> None:
        log.debug("TX %s", line)
        self.port.write((line + "\n").encode())

    def readline(self) -> str | None:
        chunk = self.port.read(256)
        if chunk:
            self._rx_buffer += chunk
        if b"\n" not in self._rx_buffer:
            return None
        line, _, self._rx_buffer = self._rx_buffer.partition(b"\n")
        return line.decode(errors="replace").strip()

    # -- state push ----------------------------------------------------------

    def active_profile(self) -> Profile:
        return self.profiles.get(self.active_app, self.profiles["default"])

    def push_focus(self, app_id: str, title: str) -> None:
        self.send(f"DECK FOCUS {app_id}\t{title}")

    def push_layout(self) -> None:
        profile = self.active_profile()
        page = profile.page(self.page_index)
        labels = [button.label for button in page]
        labels += [""] * (PAGE_SIZE - len(labels))
        icons = [button.icon or "" for button in page]
        icons += [""] * (PAGE_SIZE - len(icons))
        colors = [button.color or "" for button in page]
        colors += [""] * (PAGE_SIZE - len(colors))
        g1, g2 = profile.page_groups(self.page_index)
        if g1 or g2:
            self.send("DECK GROUPS\t" + g1 + "\t" + g2)
        if any(icons):
            self.send("DECK ICONS\t" + "\t".join(icons))
        if any(colors):
            self.send("DECK COLORS\t" + "\t".join(colors))
        header = f"{(self.page_index % profile.page_count) + 1}/{profile.page_count}"
        self.send("DECK LAYOUT " + header + "\t" + "\t".join(labels))

    def current_override(self) -> str:
        return self.control.override() if self.control is not None else self._device_override

    def push_selection(self) -> None:
        self.send("DECK SELECTED\t" + self.current_override())

    def push_profiles(self) -> None:
        profile_ids = sorted(self.profiles)
        if (any(len(app.encode()) > MAX_PROFILE_ID_BYTES or any(c in app for c in "\t\r\n")
                for app in profile_ids)
                or len(("Follow focus\n" + "\n".join(profile_ids)).encode()) >= MAX_PROFILE_CATALOG_BYTES):
            log.error(
                "profile catalog exceeds device limits (%d bytes per ID, %d bytes total)",
                MAX_PROFILE_ID_BYTES,
                MAX_PROFILE_CATALOG_BYTES - 1,
            )
            self.send("DECK PROFILES ERROR")
            return
        self.send("DECK PROFILES BEGIN")
        for app in profile_ids:
            self.send("DECK PROFILE " + app)
        self.send("DECK PROFILES END")
        self.push_selection()

    def sync_override(self, force: bool = False) -> None:
        override = self.current_override()
        if force or override != self._applied_override:
            self._applied_override = override
            self.active_app = self._resolve_profile(self.focus_app_id or "", self.focus_title or "")
            self.page_index = 0
            self.push_selection()
            self.push_layout()
            self.publish_status()

    def _match_focus_profile(self, app_id: str, title: str = "") -> str:
        """Match focused (app_id, title) against profiles, aliases, and title rules.

        Container/launcher windows (for example Snap Steam tagging Proton games as
        ``steam_steam`` with title ``"Aethus"``) check ``match_title`` across non-container
        profiles first so game/child profiles still activate automatically.
        """
        app_match: str | None = None
        if app_id:
            if app_id in self.profiles:
                app_match = app_id
            else:
                app_low = app_id.lower()
                for pid in self.profiles:
                    if pid.lower() == app_low:
                        app_match = pid
                        break
                if app_match is None:
                    for pid, prof in self.profiles.items():
                        if any(alias.lower() == app_low for alias in prof.aliases):
                            app_match = pid
                            break

        is_container = (
            app_match is not None and app_match.lower() in CONTAINER_APP_IDS
        ) or (app_id.lower() in CONTAINER_APP_IDS if app_id else False)

        if title and (app_match is None or is_container):
            title_low = title.lower()
            best_pid = ""
            best_score = -1
            for pid, prof in self.profiles.items():
                if pid == "default" or pid.lower() in CONTAINER_APP_IDS:
                    continue
                for pat in prof.match_title:
                    pat_low = pat.lower()
                    matched = (fnmatch.fnmatchcase(title_low, pat_low)
                               if ("*" in pat_low or "?" in pat_low)
                               else (pat_low in title_low))
                    if matched and len(pat_low) > best_score:
                        best_score = len(pat_low)
                        best_pid = pid
            if best_pid:
                return best_pid

        return app_match if app_match is not None else "default"

    def _resolve_profile(self, app_id: str, title: str = "") -> str:
        """Effective profile: tray override first, then focused window match."""
        override = self.current_override()
        if override and override in self.profiles:
            return override
        return self._match_focus_profile(app_id, title)

    def publish_status(self) -> None:
        if self.control is not None:
            self.control.publish(
                transport=self.transport_name,
                active_profile=self.active_app,
                focus_app=self.focus_app_id or "",
                focus_title=self.focus_title or "",
                connected="1",
            )

    def set_focus(self, app_id: str, title: str) -> None:
        """Switch profile for (app_id, title) (full FOCUS + LAYOUT push)."""
        resolved = self._resolve_profile(app_id, title)
        if resolved != self.active_app:
            self.active_app = resolved
            self.page_index = 0
        self.focus_app_id = app_id
        self.focus_title = title
        self.push_focus(app_id, title)
        self.push_layout()
        self.publish_status()

    def update_focus(self, app_id: str, title: str) -> None:
        """Apply a focus report; re-push LAYOUT when app or matched profile changed."""
        resolved = self._resolve_profile(app_id, title)
        if app_id != self.focus_app_id or resolved != self.active_app:
            self.set_focus(app_id, title)
        elif title != self.focus_title:
            self.focus_title = title
            self.push_focus(app_id, title)
            self.publish_status()

    # -- actions -------------------------------------------------------------

    def _ensure_target_focus(self, profile: Profile) -> None:
        """For lock_target profiles, raise the profile's app before keys."""
        if (not profile.lock_target or profile.app_id == "default"
                or self.focus_watcher is None):
            return
        if (self.focus_app_id == profile.app_id
                or self._match_focus_profile(self.focus_app_id or "", self.focus_title or "") == profile.app_id):
            return
        candidates = [profile.app_id] + list(profile.aliases)
        for target in candidates:
            if self.focus_watcher.activate_app(target):
                log.info("raised %s before injecting keys", target)
                time.sleep(ACTIVATE_SETTLE)
                return
        log.warning("could not raise %s; sending keys to focused window",
                    profile.app_id)

    def _execute_button(self, button: Button, profile: Profile | None = None) -> None:
        if not button.label and not button.keys and not button.shell and not button.app:
            return
        if button.app:
            launch_desktop_app(button.app)
            log.info("pressed %r -> app %s", button.label, button.app)
        elif button.keys:
            if profile is not None:
                self._ensure_target_focus(profile)
            if button.hold_ms is not None:
                hold_s = max(10, min(button.hold_ms, 5000)) / 1000.0
                self.injector.send_chord(button.keys, hold_s=hold_s)
            else:
                self.injector.send_chord(button.keys)
            log.info("pressed %r -> keys %s", button.label, button.keys)
        elif button.shell:
            subprocess.Popen(
                button.shell,
                shell=True,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
            )
            log.info("pressed %r -> shell %s", button.label, button.shell)
        else:
            raise ValueError(f"button {button.label!r} has no action")

    def run_button(self, index: int) -> None:
        profile = self.active_profile()
        page = profile.page(self.page_index)
        if index >= len(page):
            raise ValueError(f"no button {index} on current page")
        self._execute_button(page[index], profile=profile)

    def run_launcher_button(self, index: int) -> None:
        if index < 0 or index >= len(self.launcher.apps):
            raise ValueError(f"no launcher app at index {index}")
        self._execute_button(self.launcher.apps[index])

    # -- protocol ------------------------------------------------------------

    @staticmethod
    def _clean_field(value: str | None, limit: int) -> str:
        text = "".join(c if c.isprintable() and c not in "\t\r\n" else " " for c in (value or ""))
        return text[:limit]

    def push_launcher(self) -> None:
        apps = self.launcher.apps[:MAX_LAUNCHER_APPS]
        auto_ret = 1 if self.launcher.auto_return else 0
        self.send(f"DECK LAUNCHER BEGIN\t{auto_ret}\t{len(apps)}")
        for idx, btn in enumerate(apps):
            lbl = self._clean_field(btn.label, 13)
            icon = self._clean_field(btn.icon or "app", 15)
            color = self._clean_field(btn.color or "accent", 15)
            self.send(f"DECK LAUNCHER ITEM\t{idx}\t{lbl}\t{icon}\t{color}")
        self.send("DECK LAUNCHER END")

    def handshake(self) -> None:
        self.send("DECK HELLO v1")
        self.set_focus("default", "deckd connected")
        self.push_host()
        self.push_profiles()
        self.push_launcher()

    def push_host(self) -> None:
        name = socket.gethostname().split(".")[0]
        name = "".join(c if c.isprintable() and c not in "\t\r\n" else "_" for c in name)
        self.send("DECK HOST\t" + name[:63])

    def _run_unlock_worker(self, seq: str, password_bytes: bytearray, delay_ms: int) -> None:
        try:
            self.injector.unlock_sequence(password_bytes, delay_ms=delay_ms)
            self._unlock_results.put((seq, True, ""))
        except Exception as error:
            log.warning("unlock sequence failed: %s", error)
            self._unlock_results.put((seq, False, str(error)))
        finally:
            self._unlock_busy.clear()

    def _drain_unlock_results(self) -> None:
        while True:
            try:
                seq, ok, err_msg = self._unlock_results.get_nowait()
            except queue.Empty:
                break
            if ok:
                self.send(f"DECK ACK {seq}")
            else:
                self.send(f"DECK ERR {seq} {err_msg}")

    def _handle_unlock_command(self, parts: list[str]) -> None:
        if len(parts) < 3:
            self.send("DECK ERR UNLOCK invalid_command")
            return
        subcmd = parts[2]
        if subcmd == "PAIR" and len(parts) >= 4:
            allowed = self.transport_name == "usb" or (
                self.control is not None and self.control.is_unlock_pairing_active()
            )
            if not allowed:
                self.send(
                    "DECK ERR UNLOCK pairing_locked Enable 'Allow unlock pairing' "
                    "in Deck tray (or connect USB)"
                )
                return
            if self.unlock_store is None:
                self.send("DECK ERR UNLOCK unlock_unavailable")
                return
            try:
                resp = self.unlock_store.pair_response(parts[3])
                if self.control is not None and self.control.is_unlock_pairing_active():
                    self.control.enable_unlock_pairing(0)
                self.send(f"DECK UNLOCK PAIRED {resp}")
                log.info("completed tablet unlock pairing over %s", self.transport_name)
            except Exception as error:
                log.warning("unlock pairing failed: %s", error)
                self.send(f"DECK ERR UNLOCK pair_failed {error}")
        elif subcmd == "CHALLENGE" and len(parts) >= 4:
            seq = parts[3]
            if self.unlock_store is None or not self.unlock_store.is_paired:
                self.send(f"DECK ERR {seq} not_paired")
                return
            try:
                challenge_b64 = self.unlock_store.issue_challenge(seq)
                self.send(f"DECK UNLOCK NONCE {seq} {challenge_b64}")
            except Exception as error:
                log.warning("unlock challenge failed: %s", error)
                self.send(f"DECK ERR {seq} {error}")
        elif subcmd == "EXEC" and len(parts) >= 5:
            seq, envelope_b64 = parts[3], parts[4]
            if self.unlock_store is None or not self.unlock_store.is_paired:
                self.send(f"DECK ERR {seq} not_paired")
                return
            if self._unlock_busy.is_set():
                self.send(f"DECK ERR {seq} unlock_in_progress")
                return
            if not self.injector.available:
                self.send(f"DECK ERR {seq} uinput_unavailable")
                return
            pw_buf: bytearray | None = None
            try:
                delay_ms, pw_buf = self.unlock_store.verify_and_decrypt(seq, envelope_b64)
                self._unlock_busy.set()
                worker = threading.Thread(
                    target=self._run_unlock_worker,
                    args=(seq, pw_buf, delay_ms),
                    name="deck-unlock",
                    daemon=True,
                )
                pw_buf = None  # ownership transferred to worker
                worker.start()
            except Exception as error:
                if pw_buf is not None:
                    for i in range(len(pw_buf)):
                        pw_buf[i] = 0
                self._unlock_busy.clear()
                log.warning("unlock verification failed: %s", error)
                self.send(f"DECK ERR {seq} {error}")
        else:
            self.send("DECK ERR UNLOCK invalid_command")

    def handle_line(self, line: str) -> None:
        self.last_rx = time.monotonic()
        if not line.startswith(("DECK", "OK DECK", "ERR")):
            return  # unrelated firmware output (boot logs, SDUP, ...)
        if line.startswith("DECK UNLOCK EXEC ") or line.startswith("DECK UNLOCK PAIR "):
            prefix = " ".join(line.split()[:4])
            log.debug("RX %s <redacted>", prefix)
        else:
            log.debug("RX %s", line)
        parts = line.split()

        if line.startswith("DECK UNLOCK "):
            self._handle_unlock_command(parts)
            return
        if line == "DECK SELECT" or line.startswith("DECK SELECT\t"):
            profile_id = line.partition("\t")[2]
            if profile_id and profile_id not in self.profiles:
                log.warning("device selected unknown profile %r", profile_id)
                self.send("DECK ERR SELECT unknown profile")
                self.push_selection()
                self.push_layout()
                return
            if self.control is not None:
                self.control.set_override(profile_id)
            else:
                self._device_override = profile_id
            self.sync_override(force=True)
        elif line.startswith("DECK LAUNCHER PRESS") and len(parts) >= 5:
            app_idx, seq = parts[3], parts[4]
            try:
                self.run_launcher_button(int(app_idx))
                self.send(f"DECK ACK {seq}")
            except Exception as error:
                log.warning("launcher press %s failed: %s", app_idx, error)
                self.send(f"DECK ERR {seq} {error}")
        elif line.startswith("DECK PRESS") and len(parts) >= 4:
            button_id, seq = parts[2], parts[3]
            try:
                self.run_button(int(button_id))
                self.send(f"DECK ACK {seq}")
            except Exception as error:
                log.warning("press %s failed: %s", button_id, error)
                self.send(f"DECK ERR {seq} {error}")
        elif line.startswith("DECK PAGE"):
            direction = 1 if (len(parts) > 2 and parts[2] == "NEXT") else -1
            self.page_index = (self.page_index + direction) % self.active_profile().page_count
            self.push_layout()
        elif line.startswith("DECK PONG"):
            pass  # heartbeat satisfied via last_rx
        elif line.startswith("DECK PING") and len(parts) >= 3:
            self.send(f"DECK PONG {parts[2]}")
        elif line.startswith("DECK HELLO"):
            self.push_host()
            self.push_profiles()
            self.push_focus(self.focus_app_id or "default",
                            self.focus_title or "deckd connected")
            self.push_layout()
            self.push_launcher()
        elif line.startswith("ERR"):
            log.warning("device error: %s", line)

    def tick(self) -> None:
        self._drain_unlock_results()
        self.sync_override()
        if self.profile_store is not None and self.profile_store.maybe_reload():
            if self.current_override() not in self.profiles and self.current_override():
                if self.control is not None:
                    self.control.set_override("")
                else:
                    self._device_override = ""
                self._applied_override = ""
            if self.active_app not in self.profiles:
                self.active_app = self._resolve_profile(self.focus_app_id or "")
            self.page_index = 0
            self.push_profiles()
            self.push_layout()
            self.push_launcher()
            self.publish_status()
        if self.focus_watcher is not None:
            entry = self.focus_watcher.current()
            if entry is not None:
                self.update_focus(*entry)
        now = time.monotonic()
        if now - self.last_ping >= HEARTBEAT_INTERVAL:
            self.last_ping = now
            self.send(f"DECK PING {int(now * 1000) & 0xFFFFFF:x}")


def pump_session(session: DeckSession, listener: TcpListener | None = None,
                 ble_manager: "BleManager | None" = None,
                 wifi_manager: "WifiManager | None" = None,
                 idle_timeout: float | None = None) -> None:
    """Run a session until its transport raises (or the idle timeout hits)."""
    session.handshake()
    while True:
        if session.control is not None:
            checks = {
                "usb": (session.control.usb_enabled, "USB released by user"),
                "wifi": (session.control.wifi_enabled, "WiFi disabled by user"),
                "ble": (session.control.ble_enabled, "BLE disabled by user"),
            }
            check = checks.get(session.transport_name)
            if check is not None and not check[0]():
                raise ConnectionError(check[1])
        line = session.readline()
        if line:
            session.handle_line(line)
        session.tick()
        if idle_timeout is not None and \
                time.monotonic() - session.last_rx > idle_timeout:
            raise ConnectionError(f"no RX for {idle_timeout:.0f}s")
        if listener is not None:
            listener.refuse_pending()
        if wifi_manager is not None:
            wifi_manager.reconcile()
            wifi_manager.refuse_pending()
        if ble_manager is not None:
            # Apply tray BLE on/off toggles even while a session is live.
            ble_manager.reconcile()
            ble_manager.refuse_pending()


class BleManager:
    """Starts/stops the BLE GATT listener at runtime from the control toggle.

    The desired state lives in DeckControl (set via the tray's SetBleEnabled);
    reconcile() converges the actual listener to it and publishes the "ble"
    status key. A fresh BleListener is created on each enable: close() fully
    unregisters from BlueZ, so this is safe to cycle.
    """

    def __init__(self, control: DeckControl, local_name: str,
                 adapter_path: str) -> None:
        self.control = control
        self.local_name = local_name
        self.adapter_path = adapter_path
        self.listener: "BleListener | None" = None
        self._warned_unavailable = False

    def reconcile(self) -> None:
        want = self.control.ble_enabled()
        if want and self.listener is None:
            if not HAVE_BLE:
                if not self._warned_unavailable:
                    log.warning("BLE requested but ble_deck/dbus-next "
                                "unavailable; ignoring")
                    self._warned_unavailable = True
                self.control.set_ble_enabled(False)
                self.control.publish(ble="0")
                return
            try:
                listener = BleListener(local_name=self.local_name,
                                       adapter_path=self.adapter_path)
                listener.start()
                self.listener = listener
                log.info("BLE GATT server active ('%s')", self.local_name)
                self.control.publish(ble="1")
            except Exception as error:
                log.warning("could not start BLE listener: %s", error)
                self.control.set_ble_enabled(False)
                self.control.publish(ble="0")
        elif not want and self.listener is not None:
            try:
                self.listener.close()
            except Exception as error:
                log.warning("BLE listener close failed: %s", error)
            self.listener = None
            log.info("BLE GATT server stopped")
            self.control.publish(ble="0")

    def accept(self, timeout: float = 0.0):
        return self.listener.accept(timeout) if self.listener else None

    def refuse_pending(self) -> None:
        if self.listener is not None:
            self.listener.refuse_pending()

    def close(self) -> None:
        if self.listener is not None:
            try:
                self.listener.close()
            except Exception:
                pass
            self.listener = None


class WifiManager:
    """Starts/stops the WiFi TCP listener and mDNS advertiser from control state."""

    def __init__(self, control: DeckControl, port: int, mdns: bool = True) -> None:
        self.control = control
        self.port = port
        self.mdns = mdns
        self.listener: TcpListener | None = None
        self.advertiser: MdnsAdvertiser | None = None
        self._warned_disabled = False

    def reconcile(self) -> None:
        want = self.control.wifi_enabled()
        if want and self.listener is None:
            if not self.port:
                if not self._warned_disabled:
                    log.warning("WiFi requested but --tcp-port=0 disables TCP; ignoring")
                    self._warned_disabled = True
                self.control.set_wifi_enabled(False)
                self.control.publish(wifi="0")
                return
            try:
                listener = TcpListener(self.port)
                log.info("TCP listener on 0.0.0.0:%d", listener.port)
                advertiser = MdnsAdvertiser(listener.port) if self.mdns else None
                self.listener = listener
                self.advertiser = advertiser
                self.control.publish(wifi="1")
            except Exception as error:
                log.warning("could not start WiFi TCP listener: %s", error)
                self.control.set_wifi_enabled(False)
                self.control.publish(wifi="0")
        elif not want and self.listener is not None:
            self.close()
            log.info("WiFi TCP listener stopped")
            self.control.publish(wifi="0")

    def accept(self, timeout: float = 0.0):
        return self.listener.accept(timeout) if self.listener else None

    def refuse_pending(self) -> None:
        if self.listener is not None:
            self.listener.refuse_pending()

    def close(self) -> None:
        if self.advertiser is not None:
            try:
                self.advertiser.close()
            except Exception:
                pass
            self.advertiser = None
        if self.listener is not None:
            try:
                self.listener.close()
            except Exception:
                pass
            self.listener = None


def run(args: argparse.Namespace) -> int:
    profile_store = ProfileStore(Path(args.profiles))
    profiles = profile_store.profiles
    injector = KeyInjector()
    unlock_key_arg = getattr(args, "unlock_key", None)
    unlock_store = (
        UnlockKeyStore(Path(unlock_key_arg) if unlock_key_arg else None)
        if HAVE_UNLOCK_CRYPTO and UnlockKeyStore is not None
        else None
    )
    focus_watcher = FocusWatcher(
        bus_name=args.focus_bus_name,
        object_path=args.focus_object_path,
    )
    focus_watcher.start()

    wifi_arg = getattr(args, "wifi", None)
    wifi_enabled_init = (
        bool(getattr(args, "tcp_port", 0)) if wifi_arg is None else bool(wifi_arg)
    )
    control = DeckControl(
        profiles,
        bus_name=getattr(args, "control_bus_name", "org.mmesp32.Deckd"),
        ble_enabled=bool(getattr(args, "ble", False)) and HAVE_BLE,
        usb_enabled=bool(getattr(args, "usb", False)),
        wifi_enabled=wifi_enabled_init,
    )
    control.start()

    def publish_disconnected() -> None:
        # Only emit on an actual connected -> disconnected transition.
        if control.status().get("connected") == "1":
            control.publish(transport="none", connected="0")

    wifi_manager = WifiManager(
        control,
        port=int(getattr(args, "tcp_port", TCP_PORT) or 0),
        mdns=getattr(args, "mdns", True),
    )
    wifi_manager.reconcile()

    ble_manager = BleManager(
        control,
        local_name=getattr(args, "ble_name", "DeckD"),
        adapter_path=getattr(args, "ble_adapter", "/org/bluez/hci0"))
    ble_manager.reconcile()

    last_serial_attempt = 0.0
    prev_usb_enabled = control.usb_enabled()
    SERIAL_RETRY_INTERVAL = 10.0

    try:
        while True:
            wifi_manager.reconcile()
            ble_manager.reconcile()

            usb_now = control.usb_enabled()
            if usb_now and not prev_usb_enabled:
                last_serial_attempt = 0.0
            prev_usb_enabled = usb_now

            # USB serial transport (if configured and not "none", retried periodically).
            now = time.monotonic()
            if (usb_now and args.port
                    and args.port.lower() != "none"
                    and now - last_serial_attempt >= SERIAL_RETRY_INTERVAL):
                last_serial_attempt = now
                try:
                    with serial.Serial(args.port, args.baud, timeout=0.2) as port:
                        log.info("connected to %s", args.port)
                        time.sleep(args.settle)
                        port.reset_input_buffer()
                        session = DeckSession(port, profiles, injector,
                                              focus_watcher, profile_store,
                                              control, "usb",
                                              unlock_store=unlock_store)
                        pump_session(session, ble_manager=ble_manager,
                                     wifi_manager=wifi_manager,
                                     idle_timeout=SERIAL_IDLE_TIMEOUT)
                except (serial.SerialException, ConnectionError) as error:
                    log.debug("serial session ended/unavailable (%s)", error)
                finally:
                    publish_disconnected()

            # Bluetooth BLE transport.
            ble_conn = ble_manager.accept(timeout=0.1)
            if ble_conn is not None:
                try:
                    session = DeckSession(ble_conn, profiles, injector,
                                          focus_watcher, profile_store,
                                          control, "ble",
                                          unlock_store=unlock_store)
                    pump_session(session, ble_manager=ble_manager,
                                 wifi_manager=wifi_manager,
                                 idle_timeout=BLE_IDLE_TIMEOUT)
                except (ConnectionError, OSError) as error:
                    log.info("BLE session ended (%s)", error)
                finally:
                    ble_conn.close()
                    publish_disconnected()
                continue

            # WiFi TCP transport.
            conn = wifi_manager.accept(
                timeout=0.5 if ble_manager.listener is not None else min(RECONNECT_DELAY, 0.25))
            if conn is not None:
                transport = TcpTransport(conn)
                try:
                    session = DeckSession(transport, profiles, injector,
                                          focus_watcher, profile_store,
                                          control, "wifi",
                                          unlock_store=unlock_store)
                    pump_session(session, ble_manager=ble_manager,
                                 wifi_manager=wifi_manager,
                                 idle_timeout=TCP_IDLE_TIMEOUT)
                except (ConnectionError, OSError) as error:
                    log.info("TCP session ended (%s)", error)
                finally:
                    transport.close()
                    publish_disconnected()
                continue

            if wifi_manager.listener is None and ble_manager.listener is None:
                time.sleep(0.25)
    except KeyboardInterrupt:
        log.info("shutting down")
        if control.usb_enabled() and args.port and args.port.lower() != "none":
            try:
                with serial.Serial(args.port, args.baud, timeout=0.5) as port:
                    port.write(b"DECK BYE\n")
            except serial.SerialException:
                pass
        return 0
    finally:
        ble_manager.close()
        wifi_manager.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyUSB0", help="serial port")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument(
        "--profiles",
        default=str(Path(__file__).resolve().parent / "profiles.toml"),
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=2.5,
        help="seconds to wait after opening the port (ESP32 resets on DTR)",
    )
    parser.add_argument(
        "--tcp-port",
        type=int,
        default=TCP_PORT,
        help="TCP listen port for WiFi transport (0 disables)",
    )
    parser.add_argument(
        "--wifi",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="enable WiFi TCP/mDNS transport on startup (default: false / idle)",
    )
    parser.add_argument(
        "--usb",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="enable USB serial transport on startup (default: false / idle)",
    )
    parser.add_argument(
        "--ble",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="enable Bluetooth LE GATT server transport on startup (default: false / idle)",
    )
    parser.add_argument(
        "--ble-name",
        default="DeckD-" + socket.gethostname().split(".")[0][:18],
        help="BLE advertised local name (default: DeckD-<hostname>)",
    )
    parser.add_argument(
        "--ble-adapter",
        default="/org/bluez/hci0",
        help="BlueZ adapter D-Bus path (default: /org/bluez/hci0)",
    )
    parser.add_argument(
        "--no-mdns",
        dest="mdns",
        action="store_false",
        help="do not advertise _mmdeckd._tcp via mDNS",
    )
    parser.add_argument(
        "--focus-bus-name",
        default=FOCUS_BUS_NAME,
        help="D-Bus name of the GNOME focus service",
    )
    parser.add_argument(
        "--focus-object-path",
        default=FOCUS_OBJECT_PATH,
        help="D-Bus object path of the GNOME focus service",
    )
    parser.add_argument(
        "--control-bus-name",
        default="org.mmesp32.Deckd",
        help="D-Bus name to claim for the tray/control interface",
    )
    parser.add_argument(
        "--unlock-key",
        default=None,
        help="path to host unlock pairing keypair file (default: ~/.config/mmesp32-deckd/unlock.key)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
