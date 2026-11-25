# SPDX-License-Identifier: GPL-3.0-or-later
#
# This file is part of Stream Deck.
"""Cryptographic pairing and two-layer unlock decryption for deckd.

The host stores ONLY its P-256 private key and a random 32-byte wire key in
~/.config/mmesp32-deckd/unlock.key (mode 0600). The user's OS password is NEVER
stored on the host; it is encrypted by the ESP32-S3 tablet to the host's public
key, wrapped locally on the tablet under the user's tablet PIN, and sent inside
a single-use challenge-bound AES-256-GCM envelope.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import struct
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

PAIR_INFO = b"mmdeckd-pair-v1"
WIRE_INFO = b"mmdeckd-unlock-wire-v1"
INNER_INFO = b"mmdeckd-unlock-inner-v1"
INNER_AAD = b"inner-v1"

CHALLENGE_TTL_S = 10.0
MAX_DELAY_MS = 10000
DEFAULT_DELAY_MS = 1000
P256_POINT_LEN = 65
NONCE_LEN = 12
TAG_LEN = 16


def default_unlock_key_path() -> Path:
    env_path = os.environ.get("DECKD_UNLOCK_KEY", "").strip()
    if env_path:
        return Path(env_path)
    cfg_root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return cfg_root / "mmesp32-deckd" / "unlock.key"


def _hkdf_sha256(ikm: bytes, salt: bytes, info: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt if salt else None,
        info=info,
    ).derive(ikm)


def _zeroize(buf: bytearray | None) -> None:
    if buf is not None:
        for i in range(len(buf)):
            buf[i] = 0


class UnlockKeyStore:
    """Manages host unlock keypair, pairing wrap, and single-use challenges."""

    def __init__(self, key_path: Path | None = None) -> None:
        self.key_path = Path(key_path) if key_path is not None else default_unlock_key_path()
        self._host_priv: ec.EllipticCurvePrivateKey | None = None
        self._host_pub_bytes: bytes | None = None
        self._wire_key: bytes | None = None
        self._pending_challenges: dict[str, tuple[bytes, float]] = {}
        self._load_if_present()

    @property
    def is_paired(self) -> bool:
        return (
            self._host_priv is not None
            and self._host_pub_bytes is not None
            and self._wire_key is not None
        )

    def _load_if_present(self) -> None:
        if not self.key_path.is_file():
            return
        data = json.loads(self.key_path.read_text(encoding="utf-8"))
        priv_raw = base64.b64decode(data["host_priv"])
        pub_raw = base64.b64decode(data["host_pub"])
        wire_raw = base64.b64decode(data["wire_key"])
        if len(priv_raw) != 32 or len(pub_raw) != P256_POINT_LEN or len(wire_raw) != 32:
            raise ValueError("invalid unlock key file lengths")
        priv = ec.derive_private_key(int.from_bytes(priv_raw, "big"), ec.SECP256R1())
        if priv.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint) != pub_raw:
            raise ValueError("unlock key file public/private mismatch")
        self._host_priv = priv
        self._host_pub_bytes = pub_raw
        self._wire_key = wire_raw

    def ensure_keys(self) -> None:
        if self.is_paired:
            return
        priv = ec.generate_private_key(ec.SECP256R1())
        priv_bytes = priv.private_numbers().private_value.to_bytes(32, "big")
        pub_bytes = priv.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        wire_key = secrets.token_bytes(32)
        self._save_keys(priv_bytes, pub_bytes, wire_key)
        self._host_priv = priv
        self._host_pub_bytes = pub_bytes
        self._wire_key = wire_key

    def _save_keys(self, priv_bytes: bytes, pub_bytes: bytes, wire_key: bytes) -> None:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.key_path.parent, 0o700)
        except OSError:
            pass
        payload = json.dumps(
            {
                "version": 1,
                "host_priv": base64.b64encode(priv_bytes).decode("ascii"),
                "host_pub": base64.b64encode(pub_bytes).decode("ascii"),
                "wire_key": base64.b64encode(wire_key).decode("ascii"),
            },
            indent=2,
        )
        tmp_path = self.key_path.with_suffix(".tmp")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
        except Exception:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise
        os.replace(tmp_path, self.key_path)

    def pair_response(self, tablet_eph_pub_b64: str) -> str:
        """Complete ECDH pairing and return '<host_pub_b64> <h_eph_pub_b64> <wrapped_wire_b64>'."""
        t_pub_bytes = base64.b64decode(tablet_eph_pub_b64, validate=True)
        if len(t_pub_bytes) != P256_POINT_LEN or t_pub_bytes[0] != 0x04:
            raise ValueError("invalid tablet public key")
        t_pub_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), t_pub_bytes)

        self.ensure_keys()
        assert self._host_pub_bytes is not None and self._wire_key is not None

        h_eph_priv = ec.generate_private_key(ec.SECP256R1())
        h_eph_pub_bytes = h_eph_priv.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint
        )
        shared = h_eph_priv.exchange(ec.ECDH(), t_pub_key)
        k_wrap = _hkdf_sha256(shared, b"", PAIR_INFO)
        nonce = secrets.token_bytes(NONCE_LEN)
        ct_tag = AESGCM(k_wrap).encrypt(nonce, self._wire_key, h_eph_pub_bytes + t_pub_bytes)
        wrapped = nonce + ct_tag
        return (
            f"{base64.b64encode(self._host_pub_bytes).decode('ascii')} "
            f"{base64.b64encode(h_eph_pub_bytes).decode('ascii')} "
            f"{base64.b64encode(wrapped).decode('ascii')}"
        )

    def issue_challenge(self, seq: str) -> str:
        """Issue a fresh single-use challenge nonce for sequence `seq` (base64)."""
        if not self.is_paired:
            raise RuntimeError("unlock_not_paired")
        now = time.monotonic()
        # Prune expired challenges and limit map size to avoid memory growth.
        expired = [k for k, (_, exp) in self._pending_challenges.items() if exp <= now]
        for k in expired:
            del self._pending_challenges[k]
        if len(self._pending_challenges) >= 4:
            oldest = min(self._pending_challenges, key=lambda k: self._pending_challenges[k][1])
            del self._pending_challenges[oldest]

        nonce = secrets.token_bytes(16)
        self._pending_challenges[seq] = (nonce, now + CHALLENGE_TTL_S)
        return base64.b64encode(nonce).decode("ascii")

    def verify_and_decrypt(self, seq: str, envelope_b64: str) -> tuple[int, bytearray]:
        """Verify challenge + wire AES-GCM envelope, then decrypt inner ECIES password."""
        if not self.is_paired or self._host_priv is None or self._wire_key is None:
            raise RuntimeError("unlock_not_paired")
        entry = self._pending_challenges.pop(seq, None)
        if entry is None:
            raise ValueError("missing_or_used_challenge")
        challenge, expires = entry
        if time.monotonic() > expires:
            raise ValueError("challenge_expired")

        raw = base64.b64decode(envelope_b64, validate=True)
        if len(raw) < NONCE_LEN + TAG_LEN + 2 + P256_POINT_LEN + NONCE_LEN + TAG_LEN + 1:
            raise ValueError("unlock_payload_too_short")

        nonce_wire = raw[:NONCE_LEN]
        ct_wire = raw[NONCE_LEN:]
        k_wire = _hkdf_sha256(self._wire_key, challenge, WIRE_INFO)
        wire_plain = AESGCM(k_wire).decrypt(nonce_wire, ct_wire, challenge)

        if len(wire_plain) < 2 + P256_POINT_LEN + NONCE_LEN + TAG_LEN + 1:
            raise ValueError("inner_payload_too_short")

        (delay_ms,) = struct.unpack("<H", wire_plain[:2])
        delay_ms = max(0, min(int(delay_ms), MAX_DELAY_MS))
        c_inner = wire_plain[2:]

        eph_pub_bytes = c_inner[:P256_POINT_LEN]
        if eph_pub_bytes[0] != 0x04:
            raise ValueError("invalid_inner_pubkey")
        nonce_inner = c_inner[P256_POINT_LEN : P256_POINT_LEN + NONCE_LEN]
        ct_inner = c_inner[P256_POINT_LEN + NONCE_LEN :]

        eph_pub_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), eph_pub_bytes)
        shared_inner = self._host_priv.exchange(ec.ECDH(), eph_pub_key)
        k_inner = _hkdf_sha256(shared_inner, b"", INNER_INFO)

        pw_plain = AESGCM(k_inner).decrypt(nonce_inner, ct_inner, INNER_AAD)
        return delay_ms, bytearray(pw_plain)
