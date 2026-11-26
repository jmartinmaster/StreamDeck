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

"""Local control service for deckd (D-Bus on Linux, loopback JSON on Windows).

Kept in its own module WITHOUT ``from __future__ import annotations``:
dbus-next reads D-Bus signatures from literal function annotations, and
PEP 563 stringification would corrupt them. On Linux it runs an asyncio
MessageBus in a daemon thread, mirroring FocusWatcher in deckd.py; Windows
uses the local JSON control server. Thread-safety contract:
- ``set_override``/``override`` guard a single string with a lock.
- ``publish`` is called from the daemon main thread; it stores a status
  snapshot and schedules a StatusChanged emission on the bus loop.
- ``profile_ids`` reads the live profiles dict (atomic enough under the GIL
  for a key listing).
"""

import asyncio
import logging
import sys
import threading
import time

log = logging.getLogger("deckd")

CONTROL_BUS_NAME = "org.mmesp32.Deckd"
CONTROL_OBJECT_PATH = "/org/mmesp32/Deckd"
CONTROL_INTERFACE = "org.mmesp32.Deckd1"

try:
    from dbus_next.aio import MessageBus
    from dbus_next.service import ServiceInterface, method, signal

    HAVE_DBUS = True
except ImportError:  # pragma: no cover - optional dependency
    HAVE_DBUS = False


if HAVE_DBUS:

    class _ControlInterface(ServiceInterface):
        def __init__(self, control):
            super().__init__(CONTROL_INTERFACE)
            self._control = control

        @method()
        def ListProfiles(self) -> 'as':  # noqa: F722 - dbus signature
            return self._control.profile_ids()

        @method()
        def GetStatus(self) -> 'a{ss}':  # noqa: F722
            return self._control.status()

        @method()
        def SetOverride(self, profile: 's'):  # noqa: F821
            self._control.set_override(profile)

        @method()
        def SetBleEnabled(self, enabled: 'b'):  # noqa: F821
            self._control.set_ble_enabled(enabled)

        @method()
        def SetUsbEnabled(self, enabled: 'b'):  # noqa: F821
            self._control.set_usb_enabled(enabled)

        @method()
        def SetWifiEnabled(self, enabled: 'b'):  # noqa: F821
            self._control.set_wifi_enabled(enabled)

        @method()
        def EnableUnlockPairing(self, seconds: 'u') -> 'b':  # noqa: F821
            return self._control.enable_unlock_pairing(seconds)

        @signal()
        def StatusChanged(self) -> 'a{ss}':  # noqa: F722
            return self._control.status()


class DeckControl:
    """Expose the deckd control API through the current platform's IPC."""

    def __init__(self, profiles, bus_name=CONTROL_BUS_NAME,
                 object_path=CONTROL_OBJECT_PATH, ble_enabled=False,
                 usb_enabled=False, wifi_enabled=False):
        self._profiles = profiles
        self._is_windows = sys.platform == "win32"
        self.bus_name = bus_name
        self.object_path = object_path
        self._lock = threading.Lock()
        self._override = ""
        self._ble_enabled = bool(ble_enabled) and not self._is_windows
        self._usb_enabled = bool(usb_enabled)
        self._wifi_enabled = bool(wifi_enabled)
        self._unlock_pairing_until = 0.0
        self._status = {
            "transport": "none",
            "active_profile": "default",
            "override": "",
            "focus_app": "",
            "focus_title": "",
            "connected": "0",
            "ble": "0",
            "usb": "1" if self._usb_enabled else "0",
            "wifi": "1" if self._wifi_enabled else "0",
            "unlock_pairing": "0",
        }
        self._loop = None
        self._interface = None
        self._thread = None
        self._windows_server = None

    # -- main-thread API -------------------------------------------------

    def start(self):
        if self._is_windows:
            try:
                try:
                    from win_control import WindowsControlServer
                except ImportError:  # pragma: no cover - package import fallback
                    from .win_control import WindowsControlServer
                self._windows_server = WindowsControlServer(self)
                self._windows_server.start()
            except OSError:
                log.exception("Windows control interface could not start")
            return
        if not HAVE_DBUS:
            log.warning("dbus-next not installed; control interface disabled")
            return
        self._thread = threading.Thread(
            target=self._thread_main, name="deck-control", daemon=True)
        self._thread.start()

    def override(self):
        with self._lock:
            return self._override

    def ble_enabled(self):
        """Desired BLE listener state (what the user asked for)."""
        with self._lock:
            return self._ble_enabled

    def set_ble_enabled(self, enabled):
        if self._is_windows and enabled:
            log.warning("BLE is not supported on Windows; request ignored")
            return
        with self._lock:
            self._ble_enabled = bool(enabled)
        log.info("BLE connections %s", "enabled" if enabled else "disabled")

    def usb_enabled(self):
        with self._lock:
            return self._usb_enabled

    def set_usb_enabled(self, enabled):
        with self._lock:
            self._usb_enabled = bool(enabled)
        log.info("USB connections %s", "enabled" if enabled else "released")
        self.publish(usb="1" if enabled else "0")

    def wifi_enabled(self):
        """Desired WiFi TCP/mDNS listener state (what the user asked for)."""
        with self._lock:
            return self._wifi_enabled

    def set_wifi_enabled(self, enabled):
        with self._lock:
            self._wifi_enabled = bool(enabled)
        log.info("WiFi connections %s", "enabled" if enabled else "disabled")
        self.publish(wifi="1" if enabled else "0")

    def enable_unlock_pairing(self, seconds=60):
        dur = max(0, min(int(seconds), 300))
        with self._lock:
            self._unlock_pairing_until = (time.monotonic() + dur) if dur > 0 else 0.0
        active = dur > 0
        log.info("Unlock pairing %s", f"enabled for {dur}s" if active else "disabled")
        self.publish(unlock_pairing="1" if active else "0")
        return active

    def is_unlock_pairing_active(self):
        with self._lock:
            active = time.monotonic() < self._unlock_pairing_until
            if not active and self._status.get("unlock_pairing") == "1":
                self._status["unlock_pairing"] = "0"
            return active

    def publish(self, **fields):
        """Update the status snapshot and emit StatusChanged."""
        with self._lock:
            self._status.update({k: str(v) for k, v in fields.items()})
            self._status["override"] = self._override
            if "unlock_pairing" not in fields:
                self._status["unlock_pairing"] = (
                    "1" if time.monotonic() < self._unlock_pairing_until else "0"
                )
        loop, interface = self._loop, self._interface
        if loop is not None and interface is not None:
            loop.call_soon_threadsafe(interface.StatusChanged)

    # -- IPC / shared API -------------------------------------------------

    def profile_ids(self):
        return sorted(self._profiles.keys())

    def status(self):
        with self._lock:
            snap = dict(self._status)
            snap["override"] = self._override
            if self._is_windows:
                snap["ble"] = "0"
            snap["unlock_pairing"] = (
                "1" if time.monotonic() < self._unlock_pairing_until else "0"
            )
            return snap

    def set_override(self, profile):
        with self._lock:
            if profile and profile not in self._profiles:
                log.warning("override rejected: unknown profile %r", profile)
                return
            self._override = profile
        log.info("profile override: %s", profile or "(follow focus)")
        self.publish(override=profile)

    def dispatch(self, method, args):
        """Validate and dispatch one request from the Windows loopback API."""
        if method == "ListProfiles" and not args:
            return self.profile_ids()
        if method == "GetStatus" and not args:
            return self.status()
        if method == "SetOverride" and len(args) == 1 and isinstance(args[0], str):
            profile = args[0]
            if profile and profile not in self._profiles:
                raise ValueError(f"unknown profile: {profile}")
            self.set_override(profile)
            return None
        boolean_methods = {
            "SetBleEnabled": self.set_ble_enabled,
            "SetUsbEnabled": self.set_usb_enabled,
            "SetWifiEnabled": self.set_wifi_enabled,
        }
        if method in boolean_methods and len(args) == 1 and type(args[0]) is bool:
            if method == "SetBleEnabled" and self._is_windows and args[0]:
                raise ValueError("BLE is not supported on Windows")
            boolean_methods[method](args[0])
            return None
        if method == "EnableUnlockPairing":
            raise ValueError("remote lock-screen unlock is not supported on Windows")
        raise ValueError(f"unsupported control request: {method}")

    # -- bus thread --------------------------------------------------------

    def _thread_main(self):
        try:
            asyncio.run(self._run())
        except Exception as error:  # pragma: no cover - never expected
            log.warning("control service stopped: %s", error)

    async def _run(self):
        while True:
            try:
                bus = await MessageBus().connect()
                self._interface = _ControlInterface(self)
                bus.export(self.object_path, self._interface)
                await bus.request_name(self.bus_name)
                self._loop = asyncio.get_running_loop()
                log.info("control interface at %s", self.bus_name)
                await bus.wait_for_disconnect()
            except Exception as error:
                log.warning("control service error: %s; retrying in 10s", error)
            finally:
                self._loop = None
                self._interface = None
            await asyncio.sleep(10)
