# SPDX-License-Identifier: GPL-3.0-or-later
#
# This file is part of Stream Deck.
"""Tests for host-side encrypted PC unlock pairing, challenge-response, and uinput typing."""

from __future__ import annotations

import base64
import os
import secrets
import stat
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HOST_DIR = Path(__file__).resolve().parents[1]
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402
from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402

import deckd  # noqa: E402
from deckcontrol import DeckControl  # noqa: E402
from unlock_crypto import (  # noqa: E402
    INNER_AAD,
    INNER_INFO,
    PAIR_INFO,
    WIRE_INFO,
    UnlockKeyStore,
)


class FakePort:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def read(self, _n: int) -> bytes:
        return b""

    def write(self, data: bytes) -> None:
        for line in data.decode("utf-8").splitlines():
            if line:
                self.sent.append(line)


class FakeUInput:
    def __init__(self) -> None:
        self.events: list[tuple[int, int, int]] = []
        self.syns = 0

    def write(self, etype: int, code: int, value: int) -> None:
        self.events.append((etype, code, value))

    def syn(self) -> None:
        self.syns += 1


def _hkdf(ikm: bytes, salt: bytes, info: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt if salt else None,
        info=info,
    ).derive(ikm)


def _simulate_tablet_pair(store: UnlockKeyStore) -> tuple[bytes, bytes]:
    """Run the tablet side of DECK UNLOCK PAIR / PAIRED."""
    t_priv = ec.generate_private_key(ec.SECP256R1())
    t_pub = t_priv.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    resp = store.pair_response(base64.b64encode(t_pub).decode("ascii"))
    host_pub_b64, h_eph_pub_b64, wrapped_b64 = resp.split()
    host_pub = base64.b64decode(host_pub_b64)
    h_eph_pub = base64.b64decode(h_eph_pub_b64)
    wrapped = base64.b64decode(wrapped_b64)

    h_eph_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), h_eph_pub)
    shared = t_priv.exchange(ec.ECDH(), h_eph_key)
    k_wrap = _hkdf(shared, b"", PAIR_INFO)
    wire_key = AESGCM(k_wrap).decrypt(wrapped[:12], wrapped[12:], h_eph_pub + t_pub)
    return host_pub, wire_key


def _simulate_tablet_c_inner(host_pub: bytes, password: str) -> bytes:
    """Build C_inner (tablet -> host ECIES ciphertext)."""
    eph_priv = ec.generate_private_key(ec.SECP256R1())
    eph_pub = eph_priv.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    host_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), host_pub)
    shared = eph_priv.exchange(ec.ECDH(), host_key)
    k_inner = _hkdf(shared, b"", INNER_INFO)
    nonce = secrets.token_bytes(12)
    ct = AESGCM(k_inner).encrypt(nonce, password.encode("utf-8"), INNER_AAD)
    return eph_pub + nonce + ct


def _simulate_tablet_exec_envelope(
    wire_key: bytes, challenge_b64: str, delay_ms: int, c_inner: bytes
) -> str:
    challenge = base64.b64decode(challenge_b64)
    k_wire = _hkdf(wire_key, challenge, WIRE_INFO)
    nonce = secrets.token_bytes(12)
    plain = struct.pack("<H", delay_ms) + c_inner
    ct = AESGCM(k_wire).encrypt(nonce, plain, challenge)
    return base64.b64encode(nonce + ct).decode("ascii")


class UnlockCryptoTests(unittest.TestCase):
    def test_pair_and_two_layer_decrypt_without_storing_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "unlock.key"
            store = UnlockKeyStore(key_path)
            self.assertFalse(store.is_paired)

            host_pub, wire_key = _simulate_tablet_pair(store)
            self.assertTrue(store.is_paired)
            self.assertEqual(stat.S_IMODE(os.stat(key_path).st_mode), 0o600)

            secret_pw = "CorrectHorse!99?"
            c_inner = _simulate_tablet_c_inner(host_pub, secret_pw)

            # Password must never appear in the host key file.
            self.assertNotIn(secret_pw, key_path.read_text(encoding="utf-8"))

            # Reload from disk and run challenge-response.
            reloaded = UnlockKeyStore(key_path)
            ch_b64 = reloaded.issue_challenge("42")
            env_b64 = _simulate_tablet_exec_envelope(wire_key, ch_b64, 1500, c_inner)

            delay_ms, pw_buf = reloaded.verify_and_decrypt("42", env_b64)
            self.assertEqual(delay_ms, 1500)
            self.assertEqual(bytes(pw_buf).decode("utf-8"), secret_pw)

            # Replay with same seq & envelope must fail.
            with self.assertRaises(ValueError):
                reloaded.verify_and_decrypt("42", env_b64)

    def test_wrong_wire_key_and_expired_challenge_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UnlockKeyStore(Path(tmp) / "unlock.key")
            host_pub, wire_key = _simulate_tablet_pair(store)
            c_inner = _simulate_tablet_c_inner(host_pub, "abc123")

            ch_b64 = store.issue_challenge("7")
            bad_env = _simulate_tablet_exec_envelope(secrets.token_bytes(32), ch_b64, 1000, c_inner)
            with self.assertRaises(Exception):
                store.verify_and_decrypt("7", bad_env)

            # Expired challenge
            ch2_b64 = store.issue_challenge("8")
            env2 = _simulate_tablet_exec_envelope(wire_key, ch2_b64, 1000, c_inner)
            seq_key, (nonce, _exp) = "8", store._pending_challenges["8"]
            store._pending_challenges[seq_key] = (nonce, time.monotonic() - 1.0)
            with self.assertRaises(ValueError):
                store.verify_and_decrypt("8", env2)

    def test_delay_is_clamped_to_max(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UnlockKeyStore(Path(tmp) / "unlock.key")
            host_pub, wire_key = _simulate_tablet_pair(store)
            c_inner = _simulate_tablet_c_inner(host_pub, "pw")
            ch_b64 = store.issue_challenge("1")
            env = _simulate_tablet_exec_envelope(wire_key, ch_b64, 65000, c_inner)
            delay_ms, _ = store.verify_and_decrypt("1", env)
            self.assertEqual(delay_ms, 10000)


class KeyInjectorUnlockTests(unittest.TestCase):
    def test_printable_ascii_mapping(self) -> None:
        for codepoint in range(0x20, 0x7F):
            code, shift = deckd.char_to_evdev(chr(codepoint))
            self.assertIsInstance(code, int)
            self.assertIsInstance(shift, bool)
        with self.assertRaises(ValueError):
            deckd.char_to_evdev("\n")

    def test_unlock_sequence_wakes_waits_types_and_zeros_buffer(self) -> None:
        injector = deckd.KeyInjector.__new__(deckd.KeyInjector)
        fake = FakeUInput()
        injector._uinput = fake
        injector._lock = __import__("threading").Lock()

        pw = bytearray(b"Ab!")
        with mock.patch("deckd.time.sleep") as mock_sleep:
            injector.unlock_sequence(pw, delay_ms=1250)

        # Password buffer must be wiped.
        self.assertEqual(pw, bytearray(b"\x00\x00\x00"))

        # Must sleep 1.25s for wake delay.
        sleep_args = [call.args[0] for call in mock_sleep.call_args_list]
        self.assertIn(1.25, sleep_args)

        ecodes = deckd.ecodes
        pressed_keys = [code for (_t, code, val) in fake.events if val == 1]
        # Starts with wake LeftShift, ends with Enter.
        self.assertEqual(pressed_keys[0], ecodes.KEY_LEFTSHIFT)
        self.assertEqual(pressed_keys[-1], ecodes.KEY_ENTER)
        self.assertIn(ecodes.KEY_A, pressed_keys)
        self.assertIn(ecodes.KEY_B, pressed_keys)
        self.assertIn(ecodes.KEY_1, pressed_keys)  # '!' is shifted KEY_1

    def test_lazy_uinput_recovery_after_initial_permission_error(self) -> None:
        fake = FakeUInput()
        with (
            mock.patch.object(deckd, "HAVE_EVDEV", True),
            mock.patch.object(deckd, "UInput", side_effect=PermissionError("no acl yet")),
            mock.patch.object(deckd.time, "monotonic", return_value=1.0),
        ):
            injector = deckd.KeyInjector()
            self.assertIsNone(injector._uinput)
            self.assertFalse(injector.available)

        # Simulate systemd-logind uaccess ACL becoming ready after startup.
        with (
            mock.patch.object(deckd, "HAVE_EVDEV", True),
            mock.patch.object(deckd, "UInput", return_value=fake),
            mock.patch.object(deckd.time, "monotonic", return_value=10.0),
        ):
            self.assertTrue(injector.available)
            injector.send_chord("a", hold_s=0.01)

        self.assertEqual(
            fake.events,
            [
                (deckd.ecodes.EV_KEY, deckd.ecodes.KEY_A, 1),
                (deckd.ecodes.EV_KEY, deckd.ecodes.KEY_A, 0),
            ],
        )


class DeckSessionUnlockTests(unittest.TestCase):
    def _make_session(self, transport_name: str, control: DeckControl, store: UnlockKeyStore):
        port = FakePort()
        injector = deckd.KeyInjector.__new__(deckd.KeyInjector)
        injector._uinput = FakeUInput()
        injector._lock = __import__("threading").Lock()
        profiles = {"default": deckd.Profile(app_id="default")}
        session = deckd.DeckSession(
            port, profiles, injector, control=control, transport_name=transport_name, unlock_store=store
        )
        return session, port, injector._uinput

    def test_pairing_allowed_over_usb_but_gated_over_wifi_and_ble(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UnlockKeyStore(Path(tmp) / "unlock.key")
            control = DeckControl({"default": deckd.Profile("default")})
            t_priv = ec.generate_private_key(ec.SECP256R1())
            t_pub_b64 = base64.b64encode(
                t_priv.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
            ).decode("ascii")

            wifi_sess, wifi_port, _ = self._make_session("wifi", control, store)
            wifi_sess.handle_line(f"DECK UNLOCK PAIR {t_pub_b64}")
            self.assertTrue(any("pairing_locked" in ln for ln in wifi_port.sent))
            self.assertFalse(store.is_paired)

            # Enable pairing window -> Wi-Fi pairing succeeds and auto-closes window.
            self.assertTrue(control.enable_unlock_pairing(60))
            self.assertEqual(control.status()["unlock_pairing"], "1")
            wifi_port.sent.clear()
            wifi_sess.handle_line(f"DECK UNLOCK PAIR {t_pub_b64}")
            self.assertTrue(any(ln.startswith("DECK UNLOCK PAIRED ") for ln in wifi_port.sent))
            self.assertFalse(control.is_unlock_pairing_active())

            # USB pairing works even with pairing window closed.
            store2 = UnlockKeyStore(Path(tmp) / "unlock2.key")
            usb_sess, usb_port, _ = self._make_session("usb", control, store2)
            usb_sess.handle_line(f"DECK UNLOCK PAIR {t_pub_b64}")
            self.assertTrue(any(ln.startswith("DECK UNLOCK PAIRED ") for ln in usb_port.sent))

    def test_full_session_unlock_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = UnlockKeyStore(Path(tmp) / "unlock.key")
            control = DeckControl({"default": deckd.Profile("default")})
            session, port, fake_uinput = self._make_session("usb", control, store)

            t_priv = ec.generate_private_key(ec.SECP256R1())
            t_pub = t_priv.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
            t_pub_b64 = base64.b64encode(t_pub).decode("ascii")

            session.handle_line(f"DECK UNLOCK PAIR {t_pub_b64}")
            paired_line = next(ln for ln in port.sent if ln.startswith("DECK UNLOCK PAIRED "))
            _, _, _, host_pub_b64, h_eph_b64, wrapped_b64 = paired_line.split()
            host_pub = base64.b64decode(host_pub_b64)
            h_eph = base64.b64decode(h_eph_b64)
            wrapped = base64.b64decode(wrapped_b64)
            shared = t_priv.exchange(
                ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), h_eph)
            )
            wire_key = AESGCM(_hkdf(shared, b"", PAIR_INFO)).decrypt(
                wrapped[:12], wrapped[12:], h_eph + t_pub
            )

            port.sent.clear()
            session.handle_line("DECK UNLOCK CHALLENGE 11")
            nonce_line = next(ln for ln in port.sent if ln.startswith("DECK UNLOCK NONCE 11 "))
            ch_b64 = nonce_line.split()[4]

            c_inner = _simulate_tablet_c_inner(host_pub, "MyPass1!")
            env_b64 = _simulate_tablet_exec_envelope(wire_key, ch_b64, 0, c_inner)

            port.sent.clear()
            session.handle_line(f"DECK UNLOCK EXEC 11 {env_b64}")

            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                session.tick()
                if any(ln == "DECK ACK 11" for ln in port.sent):
                    break
                time.sleep(0.01)

            self.assertIn("DECK ACK 11", port.sent)
            self.assertTrue(len(fake_uinput.events) > 0)


if __name__ == "__main__":
    unittest.main()
