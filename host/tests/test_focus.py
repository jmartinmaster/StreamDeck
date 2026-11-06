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
"""Automated tests for deckd focus awareness (Phase 3).

Run directly with the deckd venv python (no pytest required):

    tools/deckd/.venv/bin/python tools/deckd/tests/test_focus.py

Covers:
  1. FocusWatcher against a fake D-Bus focus service (own test bus name,
     GetFocus + FocusChanged delivery).
  2. End-to-end profile switch over a pty pair: a focus change fed through
     the watcher queue must produce "DECK FOCUS ..." followed by a
     "DECK LAYOUT" containing the org.gnome.TextEditor "Save" button.

Never touches /dev/ttyUSB0.
"""

from __future__ import annotations

import asyncio
import os
import pty
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import serial  # noqa: E402
from dbus_next.aio import MessageBus  # noqa: E402
from dbus_next.service import ServiceInterface, method, signal  # noqa: E402

from deckd import DeckSession, FocusWatcher, load_profiles  # noqa: E402

TEST_BUS_NAME = "org.mmesp32.DeckFocusTest"
TEST_OBJECT_PATH = "/org/mmesp32/DeckFocus"
PROFILES = Path(__file__).resolve().parents[1] / "profiles.toml"


class FakeFocusService(ServiceInterface):
    def __init__(self) -> None:
        super().__init__("org.mmesp32.DeckFocus")
        self.app_id = "initial.App"
        self.title = "initial window"
        self.activations: list[str] = []

    @method()
    def GetFocus(self) -> "ss":  # noqa: F821
        return [self.app_id, self.title]

    @method()
    def ActivateApp(self, app_id: "s") -> "b":  # noqa: F821
        self.activations.append(app_id)
        return True

    @signal()
    def FocusChanged(self) -> "ss":  # noqa: F821
        return [self.app_id, self.title]


class FakeServiceThread:
    """Runs the fake focus service on its own asyncio loop/thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.iface = FakeFocusService()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._main, daemon=True)

    def _main(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._setup())
        self.ready.set()
        self.loop.run_forever()

    async def _setup(self) -> None:
        self.bus = await MessageBus().connect()
        self.bus.export(TEST_OBJECT_PATH, self.iface)
        await self.bus.request_name(TEST_BUS_NAME)

    def start(self) -> None:
        self.thread.start()
        if not self.ready.wait(timeout=10):
            raise RuntimeError("fake focus service failed to start")

    def set_focus(self, app_id: str, title: str) -> None:
        def emit() -> None:
            self.iface.app_id = app_id
            self.iface.title = title
            self.iface.FocusChanged()

        self.loop.call_soon_threadsafe(emit)


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("timed out waiting for condition")


def test_focus_watcher(service: FakeServiceThread) -> None:
    watcher = FocusWatcher(
        bus_name=TEST_BUS_NAME,
        object_path=TEST_OBJECT_PATH,
        retry_interval=0.5,
    )
    watcher.start()

    initial = wait_for(watcher.current)
    assert initial == ("initial.App", "initial window"), initial

    service.set_focus("org.gnome.TextEditor", "notes - Text Editor")
    changed = wait_for(
        lambda: (watcher.current() or (None,))[0] == "org.gnome.TextEditor"
                and watcher.current())
    assert changed == ("org.gnome.TextEditor", "notes - Text Editor"), changed
    print("PASS test_focus_watcher")


class DummyInjector:
    available = False

    def send_chord(self, chord: str) -> None:
        raise RuntimeError("no uinput in tests")


def read_device_lines(fd: int, want, timeout: float = 5.0) -> list[str]:
    """Read lines from the pty master until predicate `want` is satisfied."""
    import select

    buf = b""
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if want(lines):
            return lines
        ready, _, _ = select.select([fd], [], [], 0.1)
        if not ready:
            continue
        buf += os.read(fd, 4096)
        while b"\n" in buf:
            line, _, buf = buf.partition(b"\n")
            lines.append(line.decode(errors="replace").strip())
    raise AssertionError(f"timed out; device received: {lines}")


def test_pty_profile_switch() -> None:
    master_fd, slave_fd = pty.openpty()
    try:
        port = serial.Serial(os.ttyname(slave_fd), timeout=0.05)
        profiles = load_profiles(PROFILES)
        watcher = FocusWatcher()  # not started: queue fed directly
        session = DeckSession(port, profiles, DummyInjector(), watcher)
        session.handshake()

        watcher.push("org.gnome.TextEditor", "notes - Text Editor")
        session.tick()

        lines = read_device_lines(
            master_fd,
            want=lambda ls: any(
                l.startswith("DECK FOCUS org.gnome.TextEditor") for l in ls)
            and any(l.startswith("DECK LAYOUT") and "Save" in l for l in ls))

        focus_idx = next(
            i for i, l in enumerate(lines)
            if l.startswith("DECK FOCUS org.gnome.TextEditor"))
        layout_line = lines[focus_idx + 1]
        assert lines[focus_idx] == \
            "DECK FOCUS org.gnome.TextEditor\tnotes - Text Editor", lines
        assert layout_line.startswith("DECK LAYOUT"), lines
        assert "Save" in layout_line and "Find" in layout_line, layout_line

        # Title-only change: FOCUS is re-pushed, LAYOUT is not.
        watcher.push("org.gnome.TextEditor", "other - Text Editor")
        session.tick()
        more = read_device_lines(
            master_fd,
            want=lambda ls: any("other - Text Editor" in l for l in ls))
        focus2 = next(l for l in more if "other - Text Editor" in l)
        assert focus2 == \
            "DECK FOCUS org.gnome.TextEditor\tother - Text Editor", more
        assert not any(l.startswith("DECK LAYOUT") for l in more), more

        port.close()
        print("PASS test_pty_profile_switch")
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def test_activate_app(service: FakeServiceThread) -> None:
    """FocusWatcher.activate_app reaches the extension over D-Bus."""
    watcher = FocusWatcher(
        bus_name=TEST_BUS_NAME,
        object_path=TEST_OBJECT_PATH,
        retry_interval=0.5,
    )
    watcher.start()
    wait_for(watcher.current)

    assert watcher.activate_app("code_code") is True
    wait_for(lambda: service.iface.activations == ["code_code"])
    print("PASS test_activate_app (D-Bus round-trip)")


class RecordingInjector:
    available = True

    def __init__(self) -> None:
        self.events: list[str] = []

    def send_chord(self, chord: str) -> None:
        self.events.append(f"chord:{chord}")


class RecordingWatcherStub:
    """Stands in for FocusWatcher in lock_target ordering tests."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def activate_app(self, app_id: str, timeout: float = 1.0) -> bool:
        self.events.append(f"activate:{app_id}")
        return True

    def current(self):
        return None


def test_lock_target_ordering() -> None:
    """lock_target profiles raise their app before keys are injected."""
    import deckd

    master_fd, slave_fd = pty.openpty()
    try:
        port = serial.Serial(os.ttyname(slave_fd), timeout=0.05)
        injector = RecordingInjector()
        watcher = RecordingWatcherStub(injector.events)
        profiles = {
            "default": deckd.Profile("default"),
            "code_code": deckd.Profile(
                "code_code",
                [deckd.Button("Save", keys="ctrl+s")],
                lock_target=True),
        }
        session = DeckSession(port, profiles, injector, watcher)

        # Focus elsewhere, profile pinned to code_code via direct state.
        session.active_app = "code_code"
        session.focus_app_id = "firefox_firefox"
        session.run_button(0)
        assert injector.events == \
            ["activate:code_code", "chord:ctrl+s"], injector.events

        # Already focused on the target: no activation round-trip.
        injector.events.clear()
        session.focus_app_id = "code_code"
        session.run_button(0)
        assert injector.events == ["chord:ctrl+s"], injector.events

        # lock_target disabled: never activates.
        injector.events.clear()
        profiles["code_code"].lock_target = False
        session.focus_app_id = "firefox_firefox"
        session.run_button(0)
        assert injector.events == ["chord:ctrl+s"], injector.events

        port.close()
        print("PASS test_lock_target_ordering (activate before chord)")
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def main() -> int:
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and \
            not Path(f"/run/user/{os.getuid()}/bus").exists():
        print("SKIP: no session D-Bus available")
        return 0
    service = FakeServiceThread()
    service.start()
    test_focus_watcher(service)
    test_pty_profile_switch()
    test_activate_app(service)
    test_lock_target_ordering()
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
