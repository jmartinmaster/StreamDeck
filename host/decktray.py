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
"""decktray - notification-area (tray) applet for deckd.

Pure D-Bus implementation of org.kde.StatusNotifierItem plus
com.canonical.dbusmenu using dbus-next (no GTK/GObject imports -- GTK3
crashes on this host). The GNOME ubuntu-appindicators extension consumes
both protocols.

Run with the Stream Deck venv python:

    .venv/bin/python decktray.py

Talks to the deckd daemon control interface (org.mmesp32.Deckd1) to show
live status and let the user pin a profile override. Survives daemon and
watcher restarts without crashing.
"""

# NOTE: do NOT add `from __future__ import annotations` here -- dbus-next
# reads D-Bus signatures from the literal annotation strings and PEP 563
# would double-quote them.

import argparse
import asyncio
import json
import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path

from dbus_next import BusType, Message, MessageType, PropertyAccess, Variant
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, dbus_property, method
from dbus_next.service import signal as dbus_signal

log = logging.getLogger("decktray")

# ---------------------------------------------------------------------------
# Defaults (overridable via CLI flags, mainly for tests)
# ---------------------------------------------------------------------------
DEFAULT_WATCHER_NAME = "org.kde.StatusNotifierWatcher"
DEFAULT_DECKD_NAME = "org.mmesp32.Deckd"
DECKD_PATH = "/org/mmesp32/Deckd"
DECKD_IFACE = "org.mmesp32.Deckd1"

SNI_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

ICON_CONNECTED = "input-keyboard"
ICON_DISCONNECTED = "input-keyboard-symbolic"

WATCHER_RETRY_SECONDS = 10.0
FOCUS_TITLE_MAX = 40

PROFILES_TOML = Path(os.environ.get(
    "DECKD_PROFILES", str(Path(__file__).resolve().parent / "profiles.toml")))
SETTINGS_JSON = Path.home() / ".config" / "deckd" / "settings.json"

# Stable menu item ids
MI_STATUS = 1
MI_FOCUS = 2
MI_SEP1 = 3
MI_AUTO = 4
MI_SEP2 = 5
MI_EDIT = 6
MI_QUIT = 7
MI_BLE = 8
MI_USB = 9
MI_WIFI = 98
MI_SYNC = 99
MI_PROFILE_BASE = 10  # profile i -> menu id MI_PROFILE_BASE + i


def _load_post_save_command():
    """Return user-configured post-save sync command from local settings, if any."""
    if not SETTINGS_JSON.is_file():
        return ""
    try:
        data = json.loads(SETTINGS_JSON.read_text(encoding="utf-8"))
        return str(data.get("post_save_command", "")).strip()
    except Exception:
        return ""

TRANSPORT_LABELS = {"usb": "USB", "wifi": "WiFi", "ble": "BLE",
                    "none": "No device"}


def _truncate(text, limit=FOCUS_TITLE_MAX):
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


# ---------------------------------------------------------------------------
# com.canonical.dbusmenu
# ---------------------------------------------------------------------------
class DBusMenu(ServiceInterface):
    """Minimal com.canonical.dbusmenu backed by TrayApp state."""

    def __init__(self, app):
        super().__init__("com.canonical.dbusmenu")
        self.app = app
        self.revision = 1

    # -- helpers ------------------------------------------------------------
    def _items(self):
        """Return ordered list of (id, props, clickable) menu items."""
        app = self.app
        items = []

        items.append((MI_STATUS, {
            "label": app.status_line(),
            "enabled": False,
        }))

        focus = app.focus_line()
        if focus:
            items.append((MI_FOCUS, {"label": focus, "enabled": False}))

        items.append((MI_SEP1, {"type": "separator"}))

        auto_checked = app.daemon_online and app.override == ""
        items.append((MI_AUTO, {
            "label": "Follow focus (auto)",
            "toggle-type": "radio",
            "toggle-state": 1 if auto_checked else 0,
            "enabled": app.daemon_online,
        }))
        for i, profile in enumerate(app.profiles):
            items.append((MI_PROFILE_BASE + i, {
                "label": profile,
                "toggle-type": "radio",
                "toggle-state": 1 if app.override == profile else 0,
                "enabled": app.daemon_online,
            }))

        items.append((MI_SEP2, {"type": "separator"}))
        items.append((MI_BLE, {
            "label": "BLE connections (wireless GATT)",
            "toggle-type": "checkmark",
            "toggle-state": 1 if app.status.get("ble", "0") == "1" else 0,
            "enabled": app.daemon_online,
        }))
        items.append((MI_USB, {
            "label": "USB connections (uncheck to release port)",
            "toggle-type": "checkmark",
            "toggle-state": 1 if app.status.get("usb", "0") == "1" else 0,
            "enabled": app.daemon_online,
        }))
        items.append((MI_WIFI, {
            "label": "WiFi connections (TCP/mDNS listener)",
            "toggle-type": "checkmark",
            "toggle-state": 1 if app.status.get("wifi", "0") == "1" else 0,
            "enabled": app.daemon_online,
        }))
        items.append((MI_EDIT, {"label": "Edit profiles\u2026"}))
        if _load_post_save_command():
            items.append((MI_SYNC, {"label": "Run sync hook"}))
        items.append((MI_QUIT, {"label": "Quit tray"}))
        return items

    @staticmethod
    def _variant_props(props, requested):
        defaults = {
            "label": ("s", ""),
            "enabled": ("b", True),
            "visible": ("b", True),
            "type": ("s", "standard"),
        }
        out = {}
        merged = {}
        for key, (sig, default) in defaults.items():
            merged[key] = (sig, props.get(key, default))
        if "toggle-type" in props:
            merged["toggle-type"] = ("s", props["toggle-type"])
            merged["toggle-state"] = ("i", props.get("toggle-state", 0))
        for key, (sig, value) in merged.items():
            if requested and key not in requested:
                continue
            out[key] = Variant(sig, value)
        return out

    def layout_changed(self):
        self.revision += 1
        self.ItemsPropertiesUpdated(
            [[item_id, self._variant_props(props, None)]
             for item_id, props in self._items()], [])
        self.LayoutUpdated(self.revision, 0)

    # -- D-Bus properties ---------------------------------------------------
    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> "u":
        return 3

    @dbus_property(access=PropertyAccess.READ)
    def TextDirection(self) -> "s":
        return "ltr"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "normal"

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "as":
        return []

    # -- D-Bus methods ------------------------------------------------------
    @method()
    def GetLayout(self, parent_id: "i", recursion_depth: "i",
                  property_names: "as") -> "u(ia{sv}av)":
        requested = set(property_names) if property_names else None
        if parent_id == 0:
            children = []
            if recursion_depth != 0:
                for item_id, props in self._items():
                    children.append(Variant("(ia{sv}av)", [
                        item_id, self._variant_props(props, requested), []]))
            root_props = {}
            if requested is None or "children-display" in requested:
                root_props["children-display"] = Variant("s", "submenu")
            return [self.revision, [0, root_props, children]]
        for item_id, props in self._items():
            if item_id == parent_id:
                return [self.revision,
                        [item_id, self._variant_props(props, requested), []]]
        return [self.revision, [parent_id, {}, []]]

    @method()
    def GetGroupProperties(self, ids: "ai", property_names: "as") -> "a(ia{sv})":
        requested = set(property_names) if property_names else None
        wanted = set(ids)
        out = []
        if not wanted or 0 in wanted:
            out.append([0, {"children-display": Variant("s", "submenu")}])
        for item_id, props in self._items():
            if not wanted or item_id in wanted:
                out.append([item_id, self._variant_props(props, requested)])
        return out

    @method()
    def GetProperty(self, item_id: "i", name: "s") -> "v":
        for iid, props in self._items():
            if iid == item_id:
                return self._variant_props(props, None).get(
                    name, Variant("s", ""))
        return Variant("s", "")

    @method()
    def Event(self, item_id: "i", event_id: "s", data: "v", timestamp: "u"):
        if event_id == "clicked":
            self.app.schedule(self.app.handle_click(item_id))

    @method()
    def EventGroup(self, events: "a(isvu)") -> "ai":
        known = {iid for iid, _ in self._items()} | {0}
        errors = []
        for item_id, event_id, data, timestamp in events:
            if item_id in known:
                self.Event(item_id, event_id, data, timestamp)
            else:
                errors.append(item_id)
        return errors

    @method()
    async def AboutToShow(self, item_id: "i") -> "b":
        before = self._items()
        await self.app.refresh_from_daemon()
        return before != self._items()

    @method()
    async def AboutToShowGroup(self, ids: "ai") -> "aiai":
        before = self._items()
        await self.app.refresh_from_daemon()
        known = {item_id for item_id, _ in self._items()} | {0}
        changed = before != self._items()
        return [[item_id for item_id in ids if changed and item_id in known],
                [item_id for item_id in ids if item_id not in known]]

    # -- D-Bus signals ------------------------------------------------------
    @dbus_signal()
    def LayoutUpdated(self, revision: "u", parent: "i") -> "ui":
        return [revision, parent]

    @dbus_signal()
    def ItemsPropertiesUpdated(self, updated: "a(ia{sv})",
                               removed: "a(ias)") -> "a(ia{sv})a(ias)":
        return [updated, removed]


# ---------------------------------------------------------------------------
# org.kde.StatusNotifierItem
# ---------------------------------------------------------------------------
class StatusNotifierItem(ServiceInterface):
    def __init__(self, app):
        super().__init__("org.kde.StatusNotifierItem")
        self.app = app

    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> "s":
        return "ApplicationStatus"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> "s":
        return "deckd"

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> "s":
        return "MMEsp32 Deck"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "Active"

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> "s":
        return ICON_CONNECTED if self.app.connected else ICON_DISCONNECTED

    @dbus_property(access=PropertyAccess.READ)
    def IconPixmap(self) -> "a(iiay)":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def IconAccessibleDesc(self) -> "s":
        return self.app.status_line()

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def WindowId(self) -> "i":
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconPixmap(self) -> "a(iiay)":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconPixmap(self) -> "a(iiay)":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def AttentionAccessibleDesc(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def AttentionMovieName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def XAyatanaLabel(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def XAyatanaLabelGuide(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def XAyatanaOrderingIndex(self) -> "u":
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def ToolTip(self) -> "(sa(iiay)ss)":
        return ["", [], "MMEsp32 Deck", self.app.status_line()]

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> "o":
        return MENU_PATH

    @method()
    def ContextMenu(self, x: "i", y: "i"):
        pass

    @method()
    def Activate(self, x: "i", y: "i"):
        self.app.open_gui()

    @method()
    def SecondaryActivate(self, x: "i", y: "i"):
        pass

    @method()
    def Scroll(self, delta: "i", orientation: "s"):
        pass

    @dbus_signal()
    def NewTitle(self):
        pass

    @dbus_signal()
    def NewIcon(self):
        pass

    @dbus_signal()
    def NewAttentionIcon(self):
        pass

    @dbus_signal()
    def NewOverlayIcon(self):
        pass

    @dbus_signal()
    def NewToolTip(self):
        pass

    @dbus_signal()
    def NewStatus(self, status: "s") -> "s":
        return status


# ---------------------------------------------------------------------------
# Tray application
# ---------------------------------------------------------------------------
class TrayApp:
    def __init__(self, deckd_name=DEFAULT_DECKD_NAME,
                 watcher_name=DEFAULT_WATCHER_NAME, bus=None):
        self.deckd_name = deckd_name
        self.watcher_name = watcher_name
        self.bus = bus  # may be injected by tests

        self.daemon_online = False
        self.profiles = []
        self.status = {}  # last GetStatus/StatusChanged dict
        self.connected = False
        self.override = ""

        self.menu = DBusMenu(self)
        self.sni = StatusNotifierItem(self)

        self.quit_event = asyncio.Event()
        self._watcher_kick = asyncio.Event()
        self._tasks = []
        self._refreshing = False

    # -- presentation --------------------------------------------------------
    def status_line(self):
        if not self.daemon_online:
            return "daemon offline"
        transport_key = self.status.get("transport", "none")
        if (transport_key == "none"
                and self.status.get("usb", "0") == "0"
                and self.status.get("wifi", "0") == "0"
                and self.status.get("ble", "0") == "0"):
            return "Idle \u00b7 choose connection"
        transport = TRANSPORT_LABELS.get(transport_key, "?")
        profile = self.status.get("active_profile", "?") or "?"
        return f"{transport} \u00b7 {profile}"

    def focus_line(self):
        if not self.daemon_online:
            return ""
        app_id = self.status.get("focus_app", "")
        title = self.status.get("focus_title", "")
        if not app_id and not title:
            return ""
        text = app_id or "?"
        if title:
            text += f" \u2014 {_truncate(title)}"
        return text

    # -- lifecycle ------------------------------------------------------------
    def schedule(self, coro):
        task = asyncio.ensure_future(coro)
        self._tasks.append(task)
        task.add_done_callback(self._tasks.remove)
        return task

    async def start(self):
        if self.bus is None:
            self.bus = await MessageBus(bus_type=BusType.SESSION).connect()
        self.bus.export(SNI_PATH, self.sni)
        self.bus.export(MENU_PATH, self.menu)

        self.bus.add_message_handler(self._on_message)
        await self._add_match(
            "type='signal',sender='org.freedesktop.DBus',"
            "interface='org.freedesktop.DBus',member='NameOwnerChanged',"
            f"arg0='{self.deckd_name}'")
        await self._add_match(
            "type='signal',sender='org.freedesktop.DBus',"
            "interface='org.freedesktop.DBus',member='NameOwnerChanged',"
            f"arg0='{self.watcher_name}'")
        # sender= on a well-known name is resolved by the bus daemon, so this
        # only matches signals from the current owner of the deckd name.
        await self._add_match(
            f"type='signal',sender='{self.deckd_name}',"
            f"interface='{DECKD_IFACE}',"
            f"member='StatusChanged',path='{DECKD_PATH}'")

        if await self._name_has_owner(self.deckd_name):
            await self.refresh_from_daemon(initial=True)
        self._tasks.append(asyncio.ensure_future(self._watcher_loop()))

    async def run(self):
        await self.start()
        try:
            await self.quit_event.wait()
        finally:
            await self.stop()

    async def stop(self):
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        if self.bus is not None:
            try:
                self.bus.unexport(SNI_PATH, self.sni)
                self.bus.unexport(MENU_PATH, self.menu)
            except Exception:
                pass
            self.bus.disconnect()

    # -- low-level D-Bus helpers ----------------------------------------------
    async def _add_match(self, rule):
        await self.bus.call(Message(
            destination="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            member="AddMatch", signature="s", body=[rule]))

    async def _name_has_owner(self, name):
        reply = await self.bus.call(Message(
            destination="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            member="NameHasOwner", signature="s", body=[name]))
        return bool(reply.body[0])

    async def _deckd_call(self, member, signature="", body=None):
        reply = await self.bus.call(Message(
            destination=self.deckd_name, path=DECKD_PATH,
            interface=DECKD_IFACE, member=member,
            signature=signature, body=body or []))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{member}: {reply.error_name}")
        return reply.body

    # -- signal dispatch --------------------------------------------------------
    def _on_message(self, msg):
        if msg.message_type != MessageType.SIGNAL:
            return
        if (msg.interface == "org.freedesktop.DBus"
                and msg.member == "NameOwnerChanged"):
            name, old, new = msg.body
            if name == self.deckd_name:
                if new:
                    log.info("deckd appeared on the bus")
                    self.schedule(self.refresh_from_daemon(initial=True))
                else:
                    log.info("deckd left the bus")
                    self._set_offline()
            elif name == self.watcher_name and new:
                log.info("StatusNotifierWatcher (re)started")
                self._watcher_kick.set()
        elif (msg.interface == DECKD_IFACE and msg.member == "StatusChanged"
              and msg.path == DECKD_PATH):
            self._apply_status(msg.body[0])

    # -- daemon state -----------------------------------------------------------
    def _set_offline(self):
        changed = self.daemon_online or self.connected
        self.daemon_online = False
        self.status = {}
        self.override = ""
        if self.connected:
            self.connected = False
            self.sni.NewIcon()
        if changed:
            self._emit_updates()

    def _apply_status(self, status):
        self.daemon_online = True
        self.status = dict(status)
        self.override = self.status.get("override", "")
        connected = self.status.get("connected", "0") == "1"
        if connected != self.connected:
            self.connected = connected
            self.sni.NewIcon()
        self._emit_updates()

    def _emit_updates(self):
        self.menu.layout_changed()
        self.sni.NewToolTip()

    async def refresh_from_daemon(self, initial=False):
        if self._refreshing:
            return
        self._refreshing = True
        try:
            profiles = (await self._deckd_call("ListProfiles"))[0]
            status = (await self._deckd_call("GetStatus"))[0]
        except Exception as exc:
            if initial or self.daemon_online:
                log.warning("deckd unreachable: %s", exc)
            self._set_offline()
            return
        finally:
            self._refreshing = False
        self.profiles = list(profiles)
        self._apply_status(status)

    # -- menu actions -------------------------------------------------------------
    def open_gui(self):
        gui_path = Path(__file__).resolve().parent / "deckgui.py"
        venv_py = Path(__file__).resolve().parent / ".venv" / "bin" / "python"
        py_exe = str(venv_py) if venv_py.is_file() else sys.executable
        try:
            subprocess.Popen([py_exe, str(gui_path)],
                             start_new_session=True,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError as exc:
            log.error("Failed to launch deckgui: %s", exc)

    async def run_sync_hook(self):
        raw_cmd = _load_post_save_command()
        if not raw_cmd:
            return
        cmd_str = raw_cmd.replace("{profiles}", shlex.quote(str(PROFILES_TOML)))
        try:
            proc = await asyncio.create_subprocess_shell(cmd_str)
            rc = await proc.wait()
            if rc != 0:
                log.error("Sync hook exited with code %d", rc)
            else:
                log.info("Sync hook completed")
        except Exception as exc:
            log.error("Sync hook failed: %s", exc)

    async def handle_click(self, item_id):
        if item_id == MI_QUIT:
            self.quit_event.set()
            return
        if item_id == MI_EDIT:
            self.open_gui()
            return
        if item_id == MI_SYNC:
            self.schedule(self.run_sync_hook())
            return
        if item_id == MI_AUTO:
            await self._set_override("")
            return
        transport_toggles = {
            MI_BLE: ("ble", "SetBleEnabled"),
            MI_USB: ("usb", "SetUsbEnabled"),
            MI_WIFI: ("wifi", "SetWifiEnabled"),
        }
        if item_id in transport_toggles:
            key, method = transport_toggles[item_id]
            await self._toggle_transport(key, method)
            return
        idx = item_id - MI_PROFILE_BASE
        if 0 <= idx < len(self.profiles):
            await self._set_override(self.profiles[idx])

    async def _toggle_transport(self, key, method):
        enable = self.status.get(key, "0") != "1"
        try:
            await self._deckd_call(method, "b", [enable])
        except Exception as exc:
            log.warning("%s(%r) failed: %s", method, enable, exc)
            self._set_offline()

    async def _set_override(self, profile):
        try:
            await self._deckd_call("SetOverride", "s", [profile])
        except Exception as exc:
            log.warning("SetOverride(%r) failed: %s", profile, exc)
            self._set_offline()

    # -- watcher registration --------------------------------------------------
    async def _watcher_loop(self):
        while True:
            self._watcher_kick.clear()
            registered = await self._try_register()
            if registered:
                log.info("registered with %s", self.watcher_name)
                await self._watcher_kick.wait()
                continue
            # Watcher absent/broken: retry every 10 s, or sooner if it appears.
            try:
                await asyncio.wait_for(self._watcher_kick.wait(),
                                       WATCHER_RETRY_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _try_register(self):
        try:
            reply = await self.bus.call(Message(
                destination=self.watcher_name,
                path="/StatusNotifierWatcher",
                interface="org.kde.StatusNotifierWatcher",
                member="RegisterStatusNotifierItem",
                signature="s", body=[self.bus.unique_name]))
            return reply.message_type != MessageType.ERROR
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="deckd tray applet (SNI)")
    parser.add_argument("--deckd-name", default=DEFAULT_DECKD_NAME,
                        help="bus name of the deckd daemon")
    parser.add_argument("--watcher-name", default=DEFAULT_WATCHER_NAME,
                        help="bus name of the StatusNotifierWatcher")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


async def amain(args):
    app = TrayApp(deckd_name=args.deckd_name, watcher_name=args.watcher_name)
    await app.run()


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
