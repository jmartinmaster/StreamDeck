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
"""Tests for the deckd D-Bus control interface (deckcontrol.py).

Run directly: tools/deckd/.venv/bin/python tests/test_control.py
"""

import asyncio
import os
import pty
import sys
import time
from unittest.mock import Mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import serial  # noqa: E402

import deckd  # noqa: E402
from deckcontrol import CONTROL_INTERFACE, CONTROL_OBJECT_PATH, DeckControl  # noqa: E402

from dbus_next.aio import MessageBus  # noqa: E402

TEST_BUS_NAME = f"org.mmesp32.DeckdTest{os.getpid()}"


def wait_for(predicate, timeout=5.0, interval=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


def make_profiles():
    return {
        "default": deckd.Profile("default", [deckd.Button("Term", shell="true")]),
        "code": deckd.Profile("code", [deckd.Button("Save", keys="ctrl+s")]),
    }


async def control_client_checks(control):
    bus = await MessageBus().connect()
    try:
        intro = await bus.introspect(TEST_BUS_NAME, CONTROL_OBJECT_PATH)
        proxy = bus.get_proxy_object(TEST_BUS_NAME, CONTROL_OBJECT_PATH, intro)
        iface = proxy.get_interface(CONTROL_INTERFACE)

        profiles = await iface.call_list_profiles()
        assert profiles == ["code", "default"], profiles

        status = await iface.call_get_status()
        assert status["connected"] == "0", status
        assert status["override"] == "", status

        events = []
        iface.on_status_changed(lambda s: events.append(s))

        await iface.call_set_override("code")
        assert control.override() == "code"

        # Unknown profile must be rejected (override unchanged).
        await iface.call_set_override("nope")
        assert control.override() == "code"

        control.publish(transport="usb", connected="1",
                        active_profile="code")
        end = asyncio.get_event_loop().time() + 3
        while not events and asyncio.get_event_loop().time() < end:
            await asyncio.sleep(0.05)
        assert events, "StatusChanged not received"
        assert events[-1]["transport"] == "usb", events[-1]
        assert events[-1]["override"] == "code", events[-1]

        await iface.call_set_override("")
        assert control.override() == ""

        # Default IDLE transport state & round-trips (BLE / USB / WiFi).
        assert status["ble"] == "0" and status["usb"] == "0" and status["wifi"] == "0", status
        assert control.ble_enabled() is False
        assert control.usb_enabled() is False
        assert control.wifi_enabled() is False

        await iface.call_set_ble_enabled(True)
        assert control.ble_enabled() is True
        await iface.call_set_ble_enabled(False)
        assert control.ble_enabled() is False

        await iface.call_set_usb_enabled(True)
        assert control.usb_enabled() is True
        assert control.status()["usb"] == "1"
        await iface.call_set_usb_enabled(False)
        assert control.usb_enabled() is False
        assert control.status()["usb"] == "0"

        await iface.call_set_wifi_enabled(True)
        assert control.wifi_enabled() is True
        await iface.call_set_wifi_enabled(False)
        assert control.wifi_enabled() is False
    finally:
        bus.disconnect()


def test_control_interface():
    profiles = make_profiles()
    control = DeckControl(profiles, bus_name=TEST_BUS_NAME)
    control.start()
    assert wait_for(lambda: control._interface is not None), \
        "control service did not come up"
    asyncio.run(control_client_checks(control))
    print("PASS control interface (ListProfiles/GetStatus/SetOverride/StatusChanged)")


def test_session_override():
    """Override via control switches the pushed layout on a live session."""
    profiles = make_profiles()
    control = DeckControl(profiles, bus_name=TEST_BUS_NAME + ".S")
    # Not started: override() works without a bus for this test.

    master_fd, slave_fd = pty.openpty()
    port = serial.Serial(os.ttyname(slave_fd), 115200, timeout=0.05)
    try:
        session = deckd.DeckSession(port, profiles, deckd.KeyInjector(),
                                    control=control, transport_name="usb")
        session.handshake()

        control.set_override("code")
        session.tick()
        control.set_override("")
        session.tick()

        time.sleep(0.2)
        data = os.read(master_fd, 4096).decode()
        lines = [l for l in data.split("\n") if l.startswith("DECK LAYOUT")]
        assert len(lines) >= 3, lines
        assert "Term" in lines[0], lines[0]      # handshake: default profile
        assert "Save" in lines[1], lines[1]      # override -> code
        assert "Term" in lines[2], lines[2]      # cleared -> back to default
    finally:
        port.close()
        os.close(master_fd)
        os.close(slave_fd)
    print("PASS session override (default -> code -> default layouts)")


def test_ble_manager_reconcile():
    """BleManager starts/stops a listener to match the control toggle."""
    events = []

    class StubListener:
        def __init__(self, local_name, adapter_path):
            events.append("create")

        def start(self):
            events.append("start")

        def close(self):
            events.append("close")

    profiles = make_profiles()
    control = DeckControl(profiles, bus_name=TEST_BUS_NAME + ".B",
                          ble_enabled=False)
    # Not started: no bus needed for desired-state logic.

    original = (deckd.HAVE_BLE, getattr(deckd, "BleListener", None))
    deckd.HAVE_BLE = True
    deckd.BleListener = StubListener
    try:
        manager = deckd.BleManager(control, "DeckD", "/org/bluez/hci0")
        manager.reconcile()
        assert events == [] and manager.listener is None

        control.set_ble_enabled(True)
        manager.reconcile()
        assert events == ["create", "start"], events
        assert control.status()["ble"] == "1"
        manager.reconcile()  # steady state: no churn
        assert events == ["create", "start"], events

        control.set_ble_enabled(False)
        manager.reconcile()
        assert events == ["create", "start", "close"], events
        assert manager.listener is None
        assert control.status()["ble"] == "0"

        # Re-enable builds a fresh listener.
        control.set_ble_enabled(True)
        manager.reconcile()
        assert events == ["create", "start", "close", "create", "start"], events
        manager.close()
    finally:
        deckd.HAVE_BLE, blecls = original[0], original[1]
        if blecls is not None:
            deckd.BleListener = blecls
    print("PASS BLE manager reconcile (runtime enable/disable/re-enable)")

def test_wifi_manager_reconcile():
    """WifiManager starts/stops TcpListener + MdnsAdvertiser to match control toggle."""
    events = []

    class StubTcp:
        def __init__(self, port):
            self.port = port
            events.append(("tcp_create", port))

        def close(self):
            events.append(("tcp_close", self.port))

    class StubMdns:
        def __init__(self, port):
            self.port = port
            events.append(("mdns_create", port))

        def close(self):
            events.append(("mdns_close", self.port))

    control = DeckControl(make_profiles(), bus_name=TEST_BUS_NAME + ".W")
    orig_tcp, orig_mdns = deckd.TcpListener, deckd.MdnsAdvertiser
    deckd.TcpListener = StubTcp
    deckd.MdnsAdvertiser = StubMdns
    try:
        manager = deckd.WifiManager(control, port=4650, mdns=True)
        manager.reconcile()
        assert events == [] and manager.listener is None

        control.set_wifi_enabled(True)
        manager.reconcile()
        assert events == [("tcp_create", 4650), ("mdns_create", 4650)], events
        assert control.status()["wifi"] == "1"
        manager.reconcile()  # steady state
        assert len(events) == 2

        control.set_wifi_enabled(False)
        manager.reconcile()
        assert events == [
            ("tcp_create", 4650), ("mdns_create", 4650),
            ("mdns_close", 4650), ("tcp_close", 4650),
        ], events
        assert manager.listener is None
        assert control.status()["wifi"] == "0"
        manager.close()
    finally:
        deckd.TcpListener, deckd.MdnsAdvertiser = orig_tcp, orig_mdns
    print("PASS WiFi manager reconcile (runtime enable/disable)")


def test_usb_release():
    control = DeckControl(make_profiles(), ble_enabled=True, usb_enabled=True,
                          wifi_enabled=True)
    port = Mock()
    port.read.return_value = b""
    port.write.side_effect = lambda _: control.set_usb_enabled(False)
    session = deckd.DeckSession(port, make_profiles(), Mock(),
                                control=control, transport_name="usb")
    try:
        deckd.pump_session(session)
    except ConnectionError as error:
        assert str(error) == "USB released by user"
    else:
        raise AssertionError("USB session did not exit")
    port.read.assert_not_called()
    assert control.ble_enabled(), "USB release disabled BLE"
    assert control.wifi_enabled(), "USB release disabled WiFi"

    # Wireless sessions continue to process a heartbeat with USB released.
    wireless = deckd.DeckSession(Mock(), make_profiles(), Mock(),
                                 control=control, transport_name="ble")
    wireless.handle_line("DECK PING abc")
    wireless.port.write.assert_called_with(b"DECK PONG abc\n")

    # Disabling WiFi also cleanly terminates an active WiFi session.
    wifi_port = Mock()
    wifi_port.read.return_value = b""
    wifi_port.write.side_effect = lambda _: control.set_wifi_enabled(False)
    wifi_sess = deckd.DeckSession(wifi_port, make_profiles(), Mock(),
                                  control=control, transport_name="wifi")
    try:
        deckd.pump_session(wifi_sess)
    except ConnectionError as error:
        assert str(error) == "WiFi disabled by user"
    else:
        raise AssertionError("WiFi session did not exit")
    print("PASS USB/WiFi release exits matching session cleanly")


if __name__ == "__main__":
    test_control_interface()
    test_session_override()
    test_ble_manager_reconcile()
    test_wifi_manager_reconcile()
    test_usb_release()
    print("ALL TESTS PASSED")
