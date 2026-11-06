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

"""D-Bus control service for deckd (org.mmesp32.Deckd).

Kept in its own module WITHOUT ``from __future__ import annotations``:
dbus-next reads D-Bus signatures from literal function annotations, and
PEP 563 stringification would corrupt them.

Runs an asyncio MessageBus in a daemon thread, mirroring FocusWatcher in
deckd.py. Thread-safety contract:
- ``set_override``/``override`` guard a single string with a lock.
- ``publish`` is called from the daemon main thread; it stores a status
  snapshot and schedules a StatusChanged emission on the bus loop.
- ``profile_ids`` reads the live profiles dict (atomic enough under the GIL
  for a key listing).
"""

import asyncio
import logging
import threading

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

        @signal()
        def StatusChanged(self) -> 'a{ss}':  # noqa: F722
            return self._control.status()


class DeckControl:
    """Exports the deckd control interface on the session bus."""

    def __init__(self, profiles, bus_name=CONTROL_BUS_NAME,
                 object_path=CONTROL_OBJECT_PATH, ble_enabled=False):
        self._profiles = profiles
        self.bus_name = bus_name
        self.object_path = object_path
        self._lock = threading.Lock()
        self._override = ""
        self._ble_enabled = bool(ble_enabled)
        self._usb_enabled = True
        self._status = {
            "transport": "none",
            "active_profile": "default",
            "override": "",
            "focus_app": "",
            "focus_title": "",
            "connected": "0",
            "ble": "0",
            "usb": "1",
        }
        self._loop = None
        self._interface = None
        self._thread = None

    # -- main-thread API -------------------------------------------------

    def start(self):
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

    def publish(self, **fields):
        """Update the status snapshot and emit StatusChanged."""
        with self._lock:
            self._status.update({k: str(v) for k, v in fields.items()})
            self._status["override"] = self._override
        loop, interface = self._loop, self._interface
        if loop is not None and interface is not None:
            loop.call_soon_threadsafe(interface.StatusChanged)

    # -- bus-thread / shared API ------------------------------------------

    def profile_ids(self):
        return sorted(self._profiles.keys())

    def status(self):
        with self._lock:
            return dict(self._status)

    def set_override(self, profile):
        with self._lock:
            if profile and profile not in self._profiles:
                log.warning("override rejected: unknown profile %r", profile)
                return
            self._override = profile
        log.info("profile override: %s", profile or "(follow focus)")

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
