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

"""Bluetooth Low Energy (BLE) GATT server for deckd on Linux (BlueZ via dbus-next).

Implements the Nordic UART Service (NUS):
  Service UUID: 6E400001-B5A3-F393-E0A9-E50E24DCCA9E
  RX Char UUID:  6E400002-B5A3-F393-E0A9-E50E24DCCA9E (device -> host write)
  TX Char UUID:  6E400003-B5A3-F393-E0A9-E50E24DCCA9E (host -> device notify)
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from typing import Any

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType, PropertyAccess
from dbus_next.service import ServiceInterface, dbus_property, method

log = logging.getLogger("deckd.ble")

NUS_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
NUS_RX_CHAR_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
NUS_TX_CHAR_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

CHUNK_SIZE = 120  # Safe with ATT MTU >= 123 (ESP32 negotiates MTU 256)


class BleAppManager(ServiceInterface):
    def __init__(self, managed_objects: dict[str, dict[str, dict[str, Any]]]) -> None:
        super().__init__("org.freedesktop.DBus.ObjectManager")
        self.managed_objects = managed_objects

    @method()
    def GetManagedObjects(self) -> "a{oa{sa{sv}}}":
        return self.managed_objects


class BleGattService(ServiceInterface):
    def __init__(self, path: str, uuid: str, primary: bool = True) -> None:
        super().__init__("org.bluez.GattService1")
        self.path = path
        self.uuid = uuid
        self.primary = primary
        self.char_paths: list[str] = []

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> "b":
        return self.primary

    @dbus_property(access=PropertyAccess.READ)
    def Characteristics(self) -> "ao":
        return self.char_paths


class BleRxCharacteristic(ServiceInterface):
    """Device writes commands to this characteristic (write / write-without-response)."""

    def __init__(self, path: str, service_path: str, rx_queue: queue.Queue[bytes], on_connect_cb) -> None:
        super().__init__("org.bluez.GattCharacteristic1")
        self.path = path
        self.service_path = service_path
        self.rx_queue = rx_queue
        self.on_connect_cb = on_connect_cb
        self._value = b""

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return NUS_RX_CHAR_UUID

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":
        return self.service_path

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> "ay":
        return self._value

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":
        return ["write", "write-without-response"]

    @method()
    def WriteValue(self, value: "ay", options: "a{sv}"):
        data = bytes(value)
        if data:
            self.rx_queue.put(data)
            if self.on_connect_cb:
                self.on_connect_cb()


class BleTxCharacteristic(ServiceInterface):
    """Daemon sends notifications to device via this characteristic."""

    def __init__(self, path: str, service_path: str, loop: asyncio.AbstractEventLoop, on_connect_cb) -> None:
        super().__init__("org.bluez.GattCharacteristic1")
        self.path = path
        self.service_path = service_path
        self.loop = loop
        self.on_connect_cb = on_connect_cb
        self._value = b""
        self.notifying = False

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return NUS_TX_CHAR_UUID

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":
        return self.service_path

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> "ay":
        return self._value

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":
        return ["notify"]

    @method()
    def StartNotify(self):
        self.notifying = True
        log.info("BLE client subscribed to TX notifications")
        if self.on_connect_cb:
            self.on_connect_cb()

    @method()
    def StopNotify(self):
        self.notifying = False
        log.info("BLE client stopped TX notifications")

    def notify(self, data: bytes) -> None:
        if not self.notifying:
            return
        self._value = data
        self.emit_properties_changed({"Value": data})


class BleAdvertisement(ServiceInterface):
    def __init__(self, path: str, local_name: str, service_uuid: str) -> None:
        super().__init__("org.bluez.LEAdvertisement1")
        self.path = path
        self.local_name = local_name
        self.service_uuid = service_uuid
        self._tx_power = 0

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> "s":
        return self.local_name

    @dbus_property(access=PropertyAccess.READ)
    def ServiceUUIDs(self) -> "as":
        return [self.service_uuid]

    @dbus_property(access=PropertyAccess.READWRITE)
    def TxPower(self) -> "n":
        return self._tx_power

    @TxPower.setter
    def TxPower(self, val: "n") -> None:
        self._tx_power = val

    @method()
    def Release(self):
        log.debug("BLE advertisement released by BlueZ")


class BleTransport:
    """Wraps the active BLE connection so DeckSession can read/write."""

    def __init__(self, rx_queue: queue.Queue[bytes], tx_char: BleTxCharacteristic, loop: asyncio.AbstractEventLoop) -> None:
        self.rx_queue = rx_queue
        self.tx_char = tx_char
        self.loop = loop
        self._rx_buffer = bytearray()
        self.closed = False
        self._write_lock = asyncio.Lock()

    def read(self, n: int = 256) -> bytes:
        if self.closed:
            raise ConnectionError("BLE transport closed")
        if not self._rx_buffer:
            try:
                chunk = self.rx_queue.get(timeout=0.2)
                self._rx_buffer.extend(chunk)
            except queue.Empty:
                return b""
        out = bytes(self._rx_buffer[:n])
        del self._rx_buffer[:n]
        return out

    def write(self, data: bytes) -> None:
        if self.closed:
            raise ConnectionError("BLE transport closed")
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._async_notify_all(data), self.loop
            )
        else:
            for i in range(0, len(data), CHUNK_SIZE):
                self.tx_char.notify(data[i : i + CHUNK_SIZE])

    async def _async_notify_all(self, data: bytes) -> None:
        # A profile catalog can follow a multi-packet focus line immediately.
        async with self._write_lock:
            for i in range(0, len(data), CHUNK_SIZE):
                self.tx_char.notify(data[i : i + CHUNK_SIZE])
                if i + CHUNK_SIZE < len(data):
                    await asyncio.sleep(0.015)

    def close(self) -> None:
        self.closed = True
        while not self.rx_queue.empty():
            try:
                self.rx_queue.get_nowait()
            except queue.Empty:
                break


class BleListener:
    """Non-blocking BLE GATT Server listener using BlueZ and dbus-next."""

    def __init__(self, local_name: str = "DeckD", adapter_path: str = "/org/bluez/hci0") -> None:
        self.local_name = local_name
        self.adapter_path = adapter_path
        self.rx_queue: queue.Queue[bytes] = queue.Queue()
        self.connected_event = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.bus: MessageBus | None = None
        self.tx_char: BleTxCharacteristic | None = None
        self.rx_char: BleRxCharacteristic | None = None
        self.adv: BleAdvertisement | None = None
        self.app_path = "/org/deckd/ble"
        self.adv_path = "/org/deckd/adv"
        self.started = False
        self._active_transport: BleTransport | None = None

    def _on_device_activity(self) -> None:
        self.connected_event.set()

    def start(self) -> None:
        if self.started:
            return
        self.started = True
        self.loop = asyncio.new_event_loop()

        def run_loop():
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_forever()
            finally:
                self.loop.close()

        self.thread = threading.Thread(target=run_loop, name="deckd-ble", daemon=True)
        self.thread.start()
        startup = asyncio.run_coroutine_threadsafe(self._async_start(), self.loop)
        try:
            startup.result(timeout=35.0)
        except Exception as error:
            startup.cancel()
            self.close()
            raise RuntimeError(f"BlueZ BLE startup failed: {error}") from error

    async def _async_start(self) -> None:
        try:
            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            srv_path = self.app_path + "/service0"
            rx_path = srv_path + "/char_rx"
            tx_path = srv_path + "/char_tx"

            srv = BleGattService(srv_path, NUS_SERVICE_UUID, primary=True)
            srv.char_paths = [rx_path, tx_path]

            self.rx_char = BleRxCharacteristic(rx_path, srv_path, self.rx_queue, self._on_device_activity)
            self.tx_char = BleTxCharacteristic(tx_path, srv_path, self.loop, self._on_device_activity)

            self.bus.export(srv_path, srv)
            self.bus.export(rx_path, self.rx_char)
            self.bus.export(tx_path, self.tx_char)

            managed = {
                srv_path: {
                    "org.bluez.GattService1": {
                        "UUID": Variant("s", srv.UUID),
                        "Primary": Variant("b", srv.Primary),
                        "Characteristics": Variant("ao", srv.Characteristics),
                    }
                },
                rx_path: {
                    "org.bluez.GattCharacteristic1": {
                        "UUID": Variant("s", self.rx_char.UUID),
                        "Service": Variant("o", self.rx_char.Service),
                        "Flags": Variant("as", self.rx_char.Flags),
                        "Value": Variant("ay", b""),
                    }
                },
                tx_path: {
                    "org.bluez.GattCharacteristic1": {
                        "UUID": Variant("s", self.tx_char.UUID),
                        "Service": Variant("o", self.tx_char.Service),
                        "Flags": Variant("as", self.tx_char.Flags),
                        "Value": Variant("ay", b""),
                    }
                },
            }
            app_mgr = BleAppManager(managed)
            self.bus.export(self.app_path, app_mgr)

            self.adv = BleAdvertisement(self.adv_path, self.local_name, NUS_SERVICE_UUID)
            self.bus.export(self.adv_path, self.adv)

            adapter_obj = self.bus.get_proxy_object(
                "org.bluez", self.adapter_path, await self.bus.introspect("org.bluez", self.adapter_path)
            )
            gatt_mgr = adapter_obj.get_interface("org.bluez.GattManager1")
            adv_mgr = adapter_obj.get_interface("org.bluez.LEAdvertisingManager1")

            await gatt_mgr.call_register_application(self.app_path, {})
            log.info("Registered BLE GATT application with BlueZ (%s)", NUS_SERVICE_UUID)

            await adv_mgr.call_register_advertisement(self.adv_path, {})
            log.info("Registered BLE LE advertisement '%s'", self.local_name)
        except Exception as e:
            log.warning("Failed to start BlueZ BLE GATT server: %s", e)
            raise

    def accept(self, timeout: float = 0.0) -> BleTransport | None:
        """Wait up to timeout for a device connection / initial activity."""
        if not self.started:
            return None
        if not self.connected_event.wait(timeout=max(timeout, 0.001)):
            return None
        self.connected_event.clear()
        if self._active_transport and not self._active_transport.closed:
            return None  # Session already active
        self._active_transport = BleTransport(self.rx_queue, self.tx_char, self.loop)
        log.info("BLE client accepted")
        return self._active_transport

    def refuse_pending(self) -> None:
        """Discard incoming bytes if not in a session."""
        if self._active_transport and not self._active_transport.closed:
            self.connected_event.clear()
            return
        while not self.rx_queue.empty():
            try:
                self.rx_queue.get_nowait()
            except queue.Empty:
                break
        self.connected_event.clear()

    def close(self) -> None:
        if not self.started:
            return
        self.started = False
        if self._active_transport:
            self._active_transport.close()

        if self.loop and self.bus:
            async def _cleanup():
                try:
                    adapter_obj = self.bus.get_proxy_object(
                        "org.bluez", self.adapter_path, await self.bus.introspect("org.bluez", self.adapter_path)
                    )
                    adv_mgr = adapter_obj.get_interface("org.bluez.LEAdvertisingManager1")
                    await adv_mgr.call_unregister_advertisement(self.adv_path)
                    gatt_mgr = adapter_obj.get_interface("org.bluez.GattManager1")
                    await gatt_mgr.call_unregister_application(self.app_path)
                except Exception as error:
                    log.warning("BLE unregister failed during cleanup: %s", error)
                self.bus.disconnect()

            future = asyncio.run_coroutine_threadsafe(_cleanup(), self.loop)
            try:
                future.result(timeout=2.0)
            except Exception as error:
                log.warning("BLE cleanup did not complete: %s", error)
                future.cancel()
                self.bus.disconnect()
        if self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2.0)
