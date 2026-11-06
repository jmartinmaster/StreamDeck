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
"""Automated tests for the decktray SNI/DBusMenu applet.

Run directly with the deckd venv python (no pytest required):

    tools/deckd/.venv/bin/python tools/deckd/tests/test_tray.py

Starts a FAKE org.kde.StatusNotifierWatcher and a FAKE org.mmesp32.Deckd
on the real session bus under test-scoped well-known names, launches the
tray logic in-process pointed at those names, and asserts:

  1. The tray registers itself with the watcher (unique bus name).
  2. GetLayout returns the expected menu: status line, focus line,
     separators, radio group (auto + profiles from ListProfiles),
     "Edit profiles..." and "Quit tray".
  3. Clicking a profile radio invokes SetOverride with the right id.
  4. StatusChanged from the fake daemon triggers LayoutUpdated and the
     layout/status line/radio states/icon reflect the new state.
  5. Re-registration after a watcher restart (NameOwnerChanged).

Requires a session bus (dbus-next connects to it); no real watcher,
daemon, GTK, or serial port is touched.
"""

# NOTE: no `from __future__ import annotations` -- dbus-next needs the
# literal signature strings in annotations.

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dbus_next import BusType, Message, MessageType, Variant  # noqa: E402
from dbus_next.aio import MessageBus  # noqa: E402
from dbus_next.service import ServiceInterface, method  # noqa: E402
from dbus_next.service import signal as dbus_signal  # noqa: E402

import decktray  # noqa: E402
from decktray import (  # noqa: E402
    MENU_PATH, MI_AUTO, MI_EDIT, MI_PROFILE_BASE, MI_QUIT, MI_STATUS,
    SNI_PATH, TrayApp,
)

PID = os.getpid()
FAKE_DECKD_NAME = f"org.mmesp32.test.Deckd{PID}"
FAKE_WATCHER_NAME = f"org.mmesp32.test.Watcher{PID}"

PROFILES = ["default", "org.gnome.TextEditor", "firefox"]

STATUS_USB = {
    "transport": "usb",
    "active_profile": "default",
    "override": "",
    "focus_app": "org.gnome.TextEditor",
    "focus_title": "notes.txt - Text Editor",
    "connected": "1",
}

TIMEOUT = 5.0

passed = 0


def check(cond, label):
    global passed
    if not cond:
        raise AssertionError(f"FAIL: {label}")
    passed += 1
    print(f"  ok: {label}")


async def wait_for(event, label):
    try:
        await asyncio.wait_for(event.wait(), TIMEOUT)
    except asyncio.TimeoutError:
        raise AssertionError(f"FAIL: timed out waiting for {label}")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeWatcher(ServiceInterface):
    def __init__(self):
        super().__init__("org.kde.StatusNotifierWatcher")
        self.items = []
        self.registered = asyncio.Event()

    @method()
    def RegisterStatusNotifierItem(self, service: "s"):
        self.items.append(service)
        self.registered.set()

    @method()
    def RegisterStatusNotifierHost(self, service: "s"):
        pass


class FakeDeckd(ServiceInterface):
    def __init__(self):
        super().__init__("org.mmesp32.Deckd1")
        self.overrides = []
        self.override_called = asyncio.Event()
        self.status = dict(STATUS_USB)
        self.usb_calls = []
        self.usb_called = asyncio.Event()

    @method()
    def ListProfiles(self) -> "as":
        return list(PROFILES)

    @method()
    def GetStatus(self) -> "a{ss}":
        return dict(self.status)

    @method()
    def SetOverride(self, profile: "s"):
        self.overrides.append(profile)
        self.override_called.set()

    @method()
    def SetUsbEnabled(self, enabled: "b"):
        self.usb_calls.append(enabled)
        self.status["usb"] = "1" if enabled else "0"
        self.StatusChanged(dict(self.status))
        self.usb_called.set()

    @dbus_signal()
    def StatusChanged(self, status: "a{ss}") -> "a{ss}":
        return status


# ---------------------------------------------------------------------------
# Client helpers (exercise the tray over the real bus, like GNOME would)
# ---------------------------------------------------------------------------
async def get_layout(bus, dest):
    reply = await bus.call(Message(
        destination=dest, path=MENU_PATH,
        interface="com.canonical.dbusmenu", member="GetLayout",
        signature="iias", body=[0, -1, []]))
    assert reply.message_type == MessageType.METHOD_RETURN, reply.body
    revision, root = reply.body
    items = {}
    order = []
    for child in root[2]:
        item_id, props, _children = child.value
        items[item_id] = {k: v.value for k, v in props.items()}
        order.append(item_id)
    return revision, root, items, order


async def click(bus, dest, item_id):
    reply = await bus.call(Message(
        destination=dest, path=MENU_PATH,
        interface="com.canonical.dbusmenu", member="Event",
        signature="isvu",
        body=[item_id, "clicked", Variant("s", ""), 0]))
    assert reply.message_type == MessageType.METHOD_RETURN, reply.body


async def get_sni_prop(bus, dest, name):
    reply = await bus.call(Message(
        destination=dest, path=SNI_PATH,
        interface="org.freedesktop.DBus.Properties", member="Get",
        signature="ss", body=["org.kde.StatusNotifierItem", name]))
    assert reply.message_type == MessageType.METHOD_RETURN, reply.body
    return reply.body[0].value


# ---------------------------------------------------------------------------
# Test body
# ---------------------------------------------------------------------------
async def amain():
    # Fake services share one connection.
    fake_bus = await MessageBus(bus_type=BusType.SESSION).connect()
    watcher = FakeWatcher()
    deckd = FakeDeckd()
    fake_bus.export("/StatusNotifierWatcher", watcher)
    fake_bus.export("/org/mmesp32/Deckd", deckd)
    await fake_bus.request_name(FAKE_WATCHER_NAME)
    await fake_bus.request_name(FAKE_DECKD_NAME)

    # Client connection to observe the tray's LayoutUpdated signals.
    client_bus = await MessageBus(bus_type=BusType.SESSION).connect()
    layout_updated = asyncio.Event()
    properties_updated = asyncio.Event()
    property_updates = []
    tray_name_box = {}

    def on_client_msg(msg):
        if (msg.message_type == MessageType.SIGNAL
                and msg.interface == "com.canonical.dbusmenu"
                and msg.member == "LayoutUpdated"
                and msg.sender == tray_name_box.get("name")):
            layout_updated.set()
        if (msg.message_type == MessageType.SIGNAL
                and msg.interface == "com.canonical.dbusmenu"
                and msg.member == "ItemsPropertiesUpdated"
                and msg.sender == tray_name_box.get("name")):
            property_updates.append(msg.body[0])
            properties_updated.set()

    client_bus.add_message_handler(on_client_msg)
    await client_bus.call(Message(
        destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
        interface="org.freedesktop.DBus", member="AddMatch", signature="s",
        body=["type='signal',interface='com.canonical.dbusmenu'"]))

    # Launch the tray logic in-process.
    app = TrayApp(deckd_name=FAKE_DECKD_NAME, watcher_name=FAKE_WATCHER_NAME)
    await app.start()
    tray = app.bus.unique_name
    tray_name_box["name"] = tray

    try:
        # (a) registration with the watcher -------------------------------
        print("[1] watcher registration")
        await wait_for(watcher.registered, "RegisterStatusNotifierItem")
        check(watcher.items == [tray],
              f"tray registered its unique name ({tray})")

        # (b) initial layout ----------------------------------------------
        print("[2] GetLayout structure")
        revision, root, items, order = await get_layout(client_bus, tray)
        check(root[1]["children-display"].value == "submenu",
              "root has children-display=submenu")
        check(items[MI_STATUS]["label"] == "USB \u00b7 default",
              "status line reads 'USB · default'")
        check(items[MI_STATUS]["enabled"] is False, "status line disabled")
        focus_items = [p for p in items.values()
                       if "org.gnome.TextEditor \u2014" in p.get("label", "")]
        check(len(focus_items) == 1 and focus_items[0]["enabled"] is False,
              "focus line shows app + title, disabled")
        seps = [p for p in items.values() if p.get("type") == "separator"]
        check(len(seps) == 2, "two separators present")
        check(items[MI_AUTO]["label"] == "Follow focus (auto)"
              and items[MI_AUTO]["toggle-type"] == "radio"
              and items[MI_AUTO]["toggle-state"] == 1,
              "auto radio present and checked (no override)")
        for i, profile in enumerate(PROFILES):
            item = items[MI_PROFILE_BASE + i]
            check(item["label"] == profile
                  and item["toggle-type"] == "radio"
                  and item["toggle-state"] == 0,
                  f"profile radio '{profile}' present, unchecked")
        check(items[MI_EDIT]["label"] == "Edit profiles\u2026",
              "'Edit profiles…' present")
        check(items[MI_QUIT]["label"] == "Quit tray", "'Quit tray' present")
        check(order.index(MI_AUTO) < order.index(MI_PROFILE_BASE),
              "auto radio precedes profile radios")
        icon = await get_sni_prop(client_bus, tray, "IconName")
        check(icon == decktray.ICON_CONNECTED,
              "IconName = connected icon while connected=1")
        check(await get_sni_prop(client_bus, tray, "ItemIsMenu") is True,
              "ItemIsMenu = true")
        check(await get_sni_prop(client_bus, tray, "Menu") == MENU_PATH,
              "Menu property points at the dbusmenu path")

        # (c) clicking a profile radio ------------------------------------
        print("[3] click -> SetOverride")
        target = PROFILES[1]
        await click(client_bus, tray, MI_PROFILE_BASE + 1)
        await wait_for(deckd.override_called, "SetOverride")
        check(deckd.overrides == [target],
              f"SetOverride called with '{target}'")

        deckd.override_called.clear()
        await click(client_bus, tray, MI_AUTO)
        await wait_for(deckd.override_called, "SetOverride (auto)")
        check(deckd.overrides == [target, ""],
              "clicking auto radio calls SetOverride('')")

        await click(client_bus, tray, decktray.MI_USB)
        await wait_for(deckd.usb_called, "USB release")
        check(deckd.usb_calls == [False], "USB toggle releases serial port")
        await app.refresh_from_daemon()
        deckd.usb_called.clear()
        await click(client_bus, tray, decktray.MI_USB)
        await wait_for(deckd.usb_called, "USB resume")
        check(deckd.usb_calls == [False, True], "USB toggle resumes serial polling")

        # (d) StatusChanged updates the layout ----------------------------
        print("[4] StatusChanged -> LayoutUpdated + new layout")
        layout_updated.clear()
        deckd.status = {
            "transport": "wifi",
            "active_profile": target,
            "override": target,
            "focus_app": "firefox",
            "focus_title": "A very long page title " * 5,
            "connected": "0",
        }
        deckd.StatusChanged(dict(deckd.status))
        await wait_for(layout_updated, "LayoutUpdated signal")
        revision2, _root, items, _order = await get_layout(client_bus, tray)
        check(revision2 > revision, "layout revision bumped")
        check(items[MI_STATUS]["label"] == f"WiFi \u00b7 {target}",
              "status line reads 'WiFi · <profile>'")
        check(items[MI_AUTO]["toggle-state"] == 0,
              "auto radio unchecked under override")
        check(items[MI_PROFILE_BASE + 1]["toggle-state"] == 1,
              "overridden profile radio checked")
        focus_labels = [p["label"] for p in items.values()
                        if p.get("label", "").startswith("firefox")]
        check(focus_labels and len(focus_labels[0])
              <= len("firefox \u2014 ") + decktray.FOCUS_TITLE_MAX,
              "focus title truncated")
        icon = await get_sni_prop(client_bus, tray, "IconName")
        check(icon == decktray.ICON_DISCONNECTED,
              "IconName switches to disconnected icon when connected=0")
        await wait_for(properties_updated, "ItemsPropertiesUpdated")
        latest = {item_id: {key: value.value for key, value in props.items()}
                  for item_id, props in property_updates[-1]}
        check(latest[MI_STATUS]["label"] == f"WiFi \u00b7 {target}",
              "property signal updates cached menu labels")
        check(latest[decktray.MI_USB]["enabled"],
              "property signal enables cached connection controls")

        # Simulate a stale offline menu while the daemon remains available.
        app._set_offline()
        reply = await client_bus.call(Message(
            destination=tray, path=MENU_PATH,
            interface="com.canonical.dbusmenu", member="AboutToShow",
            signature="i", body=[0]))
        check(reply.message_type == MessageType.METHOD_RETURN
              and reply.body == [True],
              "AboutToShow waits for refresh and requests changed layout")
        check(app.daemon_online and app.profiles == PROFILES,
              "menu is online before AboutToShow replies")
        app._set_offline()
        reply = await client_bus.call(Message(
            destination=tray, path=MENU_PATH,
            interface="com.canonical.dbusmenu", member="AboutToShowGroup",
            signature="ai", body=[[0, decktray.MI_USB, 999]]))
        check(reply.message_type == MessageType.METHOD_RETURN
              and reply.body == [[0, decktray.MI_USB], [999]],
              "AboutToShowGroup reports changed items and unknown IDs")

        # (e) watcher restart -> re-registration --------------------------
        print("[5] watcher restart re-registration")
        watcher.registered.clear()
        await fake_bus.release_name(FAKE_WATCHER_NAME)
        await asyncio.sleep(0.2)
        await fake_bus.request_name(FAKE_WATCHER_NAME)
        await wait_for(watcher.registered, "re-registration after restart")
        check(watcher.items[-1] == tray,
              "tray re-registered after watcher restart")
    finally:
        await app.stop()
        fake_bus.disconnect()
        client_bus.disconnect()

    print(f"\nAll {passed} checks passed.")


def main():
    asyncio.run(amain())
    return 0


if __name__ == "__main__":
    sys.exit(main())
