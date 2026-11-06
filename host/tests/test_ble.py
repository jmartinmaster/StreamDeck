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
"""Unit tests for the deckd Bluetooth LE (BLE) transport."""

from __future__ import annotations

import asyncio
import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

# Add tools/deckd to path
DECKD_DIR = Path(__file__).resolve().parent.parent
if str(DECKD_DIR) not in sys.path:
    sys.path.insert(0, str(DECKD_DIR))

from ble_deck import CHUNK_SIZE, BleListener, BleTransport, BleTxCharacteristic
from deckd import BleManager, Button, DeckSession, KeyInjector, Profile, ProfileStore


class MockTxChar:
    def __init__(self):
        self.notified: list[bytes] = []
        self.notifying = True

    def notify(self, data: bytes) -> None:
        self.notified.append(data)


class TestBleTransport(unittest.TestCase):
    def setUp(self):
        self.rx_queue: queue.Queue[bytes] = queue.Queue()
        self.mock_tx = MockTxChar()
        self.loop = asyncio.new_event_loop()
        self.transport = BleTransport(self.rx_queue, self.mock_tx, self.loop)

    def tearDown(self):
        self.transport.close()
        self.loop.close()

    def test_read_lines(self):
        self.rx_queue.put(b"DECK HELLO v1\n")
        line = self.transport.read(256)
        self.assertEqual(line, b"DECK HELLO v1\n")

    def test_notifications_fit_configured_chunk_size(self):
        data = b"DECK FOCUS code\t" + b"title" * 60 + b"\n"
        self.transport.write(data)
        self.assertEqual(b"".join(self.mock_tx.notified), data)
        self.assertTrue(all(len(chunk) <= CHUNK_SIZE for chunk in self.mock_tx.notified))

    def test_async_protocol_lines_do_not_interleave(self):
        focus = b"DECK FOCUS code\t" + b"title" * 60 + b"\n"
        catalog = b"DECK PROFILES BEGIN\n"
        async def send():
            await asyncio.gather(self.transport._async_notify_all(focus),
                                 self.transport._async_notify_all(catalog))
        self.loop.run_until_complete(send())
        self.assertEqual(b"".join(self.mock_tx.notified), focus + catalog)

    def test_hello_resends_layout_and_ping_gets_reply(self):
        profiles = {"default": Profile("default", [Button("Term", shell="true")])}
        session = DeckSession(self.transport, profiles, Mock(), transport_name="ble")
        session.handle_line("DECK HELLO v1 esp32s3-deck")
        output = b"".join(self.mock_tx.notified)
        self.assertIn(b"DECK FOCUS default\tdeckd connected\n", output)
        self.assertIn(b"DECK LAYOUT 1/1\tTerm", output)
        self.mock_tx.notified.clear()
        session.handle_line("DECK PING abc")
        self.assertEqual(b"".join(self.mock_tx.notified), b"DECK PONG abc\n")

    def test_session_handshake_and_press(self):
        profiles = {
            "default": Profile(
                "default",
                buttons=[
                    Button("Play", keys="KEY_PLAYPAUSE"),
                    Button("Mute", keys="KEY_MUTE"),
                ],
            )
        }
        injector = KeyInjector()
        session = DeckSession(self.transport, profiles, injector, transport_name="ble")

        # Simulate device sending DECK HELLO
        self.rx_queue.put(b"DECK HELLO v1 test_device\n")
        line = session.readline()
        self.assertEqual(line, "DECK HELLO v1 test_device")
        session.handle_line(line)

        # Check that session responded with layout/focus
        # (In our mock, BleTransport calls notify via loop)
        def run_loop():
            self.loop.run_until_complete(asyncio.sleep(0.05))

        # Test button press
        self.rx_queue.put(b"DECK PRESS 0 1\n")
        press_line = session.readline()
        self.assertEqual(press_line, "DECK PRESS 0 1")
        session.handle_line(press_line)


class TestBleListenerLifecycle(unittest.TestCase):
    def test_active_session_keeps_queued_commands(self):
        listener = BleListener()
        listener.started = True
        listener.tx_char = MockTxChar()
        listener.loop = asyncio.new_event_loop()
        try:
            listener.connected_event.set()
            transport = listener.accept()
            listener.rx_queue.put(b"DECK PONG abc\n")
            listener.refuse_pending()
            self.assertEqual(transport.read(), b"DECK PONG abc\n")
            transport.close()
            listener.rx_queue.put(b"stale\n")
            listener.refuse_pending()
            self.assertTrue(listener.rx_queue.empty())
        finally:
            listener.loop.close()

    def test_listener_start_and_close(self):
        listener = BleListener(local_name="DeckD-Unit")
        with patch.object(listener, "_async_start", new_callable=AsyncMock):
            listener.start()
            self.assertTrue(listener.started)
            listener.close()
        self.assertFalse(listener.started)
        self.assertFalse(listener.thread.is_alive())

    def test_advertisement_failure_is_not_reported_as_started(self):
        listener = BleListener(local_name="DeckD-Unit")
        bus = Mock()
        bus.introspect = AsyncMock()
        gatt = Mock(call_register_application=AsyncMock(),
                    call_unregister_application=AsyncMock())
        advertisement = Mock(
            call_register_advertisement=AsyncMock(
                side_effect=RuntimeError("Failed to register advertisement")),
            call_unregister_advertisement=AsyncMock())
        adapter = bus.get_proxy_object.return_value
        adapter.get_interface.side_effect = lambda name: (
            gatt if name == "org.bluez.GattManager1" else advertisement)
        with patch("ble_deck.MessageBus") as bus_type:
            bus_type.return_value.connect = AsyncMock(return_value=bus)
            with self.assertRaisesRegex(RuntimeError, "Failed to register advertisement"):
                listener.start()
        self.assertFalse(listener.started)
        self.assertFalse(listener.thread.is_alive())
        bus.disconnect.assert_called_once()
        gatt.call_unregister_application.assert_awaited_once()

    def test_manager_disables_ble_after_startup_failure(self):
        control = Mock()
        control.ble_enabled.return_value = True
        manager = BleManager(control, "DeckD-Unit", "/org/bluez/hci0")
        with patch("deckd.HAVE_BLE", True), patch("deckd.BleListener") as listener:
            listener.return_value.start.side_effect = RuntimeError("Advertisement rejected")
            manager.reconcile()
        self.assertIsNone(manager.listener)
        control.set_ble_enabled.assert_called_once_with(False)
        control.publish.assert_called_once_with(ble="0")


if __name__ == "__main__":
    unittest.main()
