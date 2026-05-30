"""Sink signing identity and Ed25519 helpers (spec §5.2, §7.1, §6).

The long-term ``sink_sig`` key (Ed25519) signs data-bearing responses
(``X-Sink-Signature``) and identifies the sink. In a real TD it is derived from
the dstack guest agent (``get_key(path="voxterm-sink/v1/sig", ...)``); in dev
mode it is derived deterministically from a seed so it survives restarts.
"""

from __future__ import annotations

import hashlib

import blake3
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)


class SinkIdentity:
    """Holds the sink's Ed25519 signing key and derived identifiers."""

    def __init__(self, private_key: Ed25519PrivateKey):
        self._sk = private_key
        self._pk = private_key.public_key()

    @classmethod
    def from_seed(cls, seed: str) -> "SinkIdentity":
        """Deterministic dev identity: Ed25519 key from a hashed seed.

        Used only in dev mode and as the NON-ATTESTED fallback when the dstack
        guest agent is unreachable. In a real TD the key comes from the guest
        agent via :meth:`from_raw` (spec §5.2).
        """
        raw = hashlib.sha256(("voxterm-sink/v1/sig::" + seed).encode()).digest()
        return cls(Ed25519PrivateKey.from_private_bytes(raw))

    @classmethod
    def from_raw(cls, private_bytes: bytes) -> "SinkIdentity":
        """Build from a 32-byte raw Ed25519 private key (e.g. from ``get_key``)."""
        return cls(Ed25519PrivateKey.from_private_bytes(private_bytes[:32]))

    @property
    def sig_pubkey_bytes(self) -> bytes:
        return self._pk.public_bytes(Encoding.Raw, PublicFormat.Raw)

    @property
    def sig_pubkey_hex(self) -> str:
        return self.sig_pubkey_bytes.hex()

    @property
    def sink_id(self) -> str:
        """20-byte BLAKE3 of the signing pubkey, hex (stable per identity)."""
        return blake3.blake3(self.sig_pubkey_bytes).hexdigest()[:40]

    def sign(self, message: bytes) -> str:
        """Return ``ed25519:<hex>`` over ``message``."""
        return "ed25519:" + self._sk.sign(message).hex()

    def response_signature(self, body: bytes) -> str:
        """X-Sink-Signature: ed25519 over BLAKE3(body) (spec §7.1)."""
        return self.sign(blake3.blake3(body).digest())


def verify_ed25519(pubkey_hex: str, signature: str, message: bytes) -> bool:
    """Verify an ``ed25519:<hex>`` signature by ``pubkey_hex`` over ``message``."""
    sig = signature[len("ed25519:") :] if signature.startswith("ed25519:") else signature
    try:
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
        pk.verify(bytes.fromhex(sig), message)
        return True
    except (InvalidSignature, ValueError):
        return False
