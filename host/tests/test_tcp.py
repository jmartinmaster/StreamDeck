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
"""Automated tests for the deckd WiFi TCP transport (Phase 4 host side).

Run directly with the deckd venv python (no pytest required):

    tools/deckd/.venv/bin/python tools/deckd/tests/test_tcp.py

Covers:
  1. Serial -> TCP failover: the daemon loop runs in a thread with a
     nonexistent serial port and must accept a TCP "device".
  2. Handshake: daemon initiates DECK HELLO, then FOCUS + LAYOUT.
  3. DECK PRESS -> shell action (touches a marker file) -> DECK ACK.
  4. Heartbeat: PING arrives within ~6 s; PONG replies keep the session
     alive past the idle timeout.
  5. Idle reaper: a silent client is dropped after the idle timeout, and
     the daemon accepts a fresh client afterwards (reconnect path).
  6. Polite refusal: TcpListener.refuse_pending sends DECK BYE and closes.

Timeouts are shortened via module constants to keep the run fast.
Never touches /dev/ttyUSB0 and never opens a real serial port.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import deckd  # noqa: E402
from deckd import FOCUS_OBJECT_PATH, TcpListener  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
PROFILES = TESTS_DIR / "_tcp_profiles.toml"
MARKER = TESTS_DIR / "_tcp_press_marker"

# Speed up the test run (daemon reads these globals on every loop pass).
deckd.RECONNECT_DELAY = 0.2
deckd.HEARTBEAT_INTERVAL = 1.0
deckd.TCP_IDLE_TIMEOUT = 3.0


class DummyInjector:
    available = False

    def send_chord(self, chord: str) -> None:
        raise RuntimeError("no uinput in tests")


class FakeDevice:
    """Line-oriented TCP client standing in for the ESP32."""

    def __init__(self, port: int, connect_timeout: float = 5.0) -> None:
        deadline = time.monotonic() + connect_timeout
        while True:
            try:
                self.sock = socket.create_connection(
                    ("127.0.0.1", port), timeout=1.0)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        self.sock.settimeout(0.1)
        self.buf = b""
        self.lines: list[str] = []
        self.eof = False
        self.connected_at = time.monotonic()

    def poll(self) -> None:
        if self.eof:
            return
        try:
            chunk = self.sock.recv(4096)
        except TimeoutError:
            return
        except OSError:
            self.eof = True
            return
        if not chunk:
            self.eof = True
            return
        self.buf += chunk
        while b"\n" in self.buf:
            line, _, self.buf = self.buf.partition(b"\n")
            self.lines.append(line.decode(errors="replace").strip())

    def wait_lines(self, predicate, timeout: float = 5.0) -> list[str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.poll()
            if predicate(self.lines):
                return self.lines
            if self.eof:
                break
        raise AssertionError(
            f"timed out (eof={self.eof}); device received: {self.lines}")

    def send(self, line: str) -> None:
        self.sock.sendall((line + "\n").encode())

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("timed out waiting for condition")


def write_profiles() -> None:
    PROFILES.write_text(
        '[app.default]\n'
        'buttons = [\n'
        f'  {{ label = "Touch", shell = "touch {MARKER}" }},\n'
        ']\n'
    )


def start_daemon(tcp_port: int) -> threading.Thread:
    deckd.KeyInjector = DummyInjector  # keep /dev/uinput untouched in tests
    args = argparse.Namespace(
        port="/dev/deckd-test-does-not-exist",
        baud=115200,
        profiles=str(PROFILES),
        settle=0.0,
        tcp_port=tcp_port,
        mdns=False,  # keep multicast off the LAN during tests
        focus_bus_name="org.mmesp32.DeckFocusTcpTestUnused",
        focus_object_path=FOCUS_OBJECT_PATH,
        control_bus_name=f"org.mmesp32.DeckdTcpTest{os.getpid()}",
    )
    thread = threading.Thread(
        target=deckd.run, args=(args,), name="deckd-under-test", daemon=True)
    thread.start()
    return thread


def test_session(port: int) -> None:
    device = FakeDevice(port)

    # 1+2. Failover + handshake: daemon speaks first.
    device.wait_lines(lambda ls: len(ls) >= 3)
    assert device.lines[0] == "DECK HELLO v1", device.lines
    assert device.lines[1].startswith("DECK FOCUS default"), device.lines
    assert device.lines[2].startswith("DECK LAYOUT"), device.lines
    assert "Touch" in device.lines[2], device.lines
    print("PASS handshake (HELLO + FOCUS + LAYOUT)")

    # 3. PRESS runs the shell action and is ACKed.
    assert not MARKER.exists()
    device.send("DECK PRESS 0 42")
    device.wait_lines(lambda ls: "DECK ACK 42" in ls)
    wait_for(MARKER.exists)
    print("PASS press -> shell action -> ACK")

    # 4. First PING within ~6 s of connect; PONGs keep the session alive
    # well past the idle timeout.
    device.wait_lines(
        lambda ls: any(l.startswith("DECK PING") for l in ls), timeout=6.0)
    first_ping_at = time.monotonic()
    assert first_ping_at - device.connected_at < 6.0
    answered = 0
    keepalive_until = time.monotonic() + 2.5 * deckd.TCP_IDLE_TIMEOUT
    while time.monotonic() < keepalive_until:
        device.poll()
        assert not device.eof, "session dropped despite PONG replies"
        while answered < len(device.lines):
            line = device.lines[answered]
            answered += 1
            if line.startswith("DECK PING"):
                device.send(f"DECK PONG {line.split()[2]}")
    pings = sum(1 for l in device.lines if l.startswith("DECK PING"))
    assert pings >= 3, device.lines
    assert not device.eof
    print(f"PASS heartbeat ({pings} PINGs answered, session alive "
          f"{2.5 * deckd.TCP_IDLE_TIMEOUT:.1f}s > idle timeout)")

    # 5. Go silent: daemon must drop us after ~TCP_IDLE_TIMEOUT.
    silent_from = time.monotonic()
    wait_for(lambda: (device.poll(), device.eof)[1],
             timeout=deckd.TCP_IDLE_TIMEOUT + 4.0)
    elapsed = time.monotonic() - silent_from
    assert elapsed >= deckd.TCP_IDLE_TIMEOUT - 1.5, elapsed
    device.close()
    print(f"PASS silent client dropped after {elapsed:.1f}s")

    # ...and the daemon returns to the accept loop for a fresh client.
    device2 = FakeDevice(port, connect_timeout=10.0)
    device2.wait_lines(lambda ls: "DECK HELLO v1" in ls, timeout=10.0)
    device2.close()
    print("PASS reconnect after drop (new client gets HELLO)")


def test_refusal() -> None:
    listener = TcpListener(0, host="127.0.0.1")
    try:
        client = socket.create_connection(("127.0.0.1", listener.port),
                                          timeout=5.0)
        client.settimeout(5.0)
        time.sleep(0.1)
        listener.refuse_pending()
        data = b""
        while True:
            chunk = client.recv(256)
            if not chunk:
                break
            data += chunk
        assert data == b"DECK BYE\n", data
        client.close()
        print("PASS busy refusal (DECK BYE + close)")
    finally:
        listener.close()


def main() -> int:
    MARKER.unlink(missing_ok=True)
    write_profiles()
    try:
        test_refusal()
        port = free_port()
        start_daemon(port)
        test_session(port)
        print("ALL TESTS PASSED")
        return 0
    finally:
        MARKER.unlink(missing_ok=True)
        PROFILES.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
