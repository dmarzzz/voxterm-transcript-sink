"""Attestation bundle production (spec §5.4) and quote providers (§5.1).

Two backends:

* ``DstackQuoteProvider`` — DEFAULT. Talks to the dstack guest agent over the
  Unix socket and returns a real TDX DCAP quote. If the socket is unreachable it
  raises ``AttestationUnavailable`` so the route returns ``503`` (spec §7.3) —
  it NEVER fabricates a bundle.
* ``DevQuoteProvider`` — opt-in via ``VOXTERM_SINK_ATTEST=dev``. Fabricates a
  self-consistent quote for local development. Carries no hardware guarantee.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Optional, Protocol

import blake3

from . import WIRE
from .identity import SinkIdentity

log = logging.getLogger("voxterm.sink")

DOMAIN = WIRE  # "voxterm-sink/1"
_ZERO32 = b"\x00" * 32
DEV_QUOTE_MAGIC = b"DEVQUOTE"

# Spec §5.2 / dstack-sdk key derivation path for the long-term signing identity.
SIG_KEY_PATH = "voxterm-sink/v1/sig"


class AttestationUnavailable(Exception):
    """The guest agent is unreachable; the route must answer 503 (§7.3)."""


def compute_report_data(
    sig_pubkey: bytes, dh_pubkey: Optional[bytes], nonce: Optional[bytes]
) -> bytes:
    """report_data per spec §5.4 — BLAKE3-512, exactly 64 bytes for REPORTDATA."""
    h = blake3.blake3()
    h.update(DOMAIN.encode() + b"\x00")
    h.update(sig_pubkey)
    h.update(dh_pubkey if dh_pubkey is not None else _ZERO32)
    h.update(nonce if nonce is not None else _ZERO32)
    return h.digest(length=64)


@dataclass
class AppInfo:
    app_id: str = ""
    instance_id: str = ""
    compose_hash: str = ""


class DstackBackend:
    """Single dstack guest-agent client shared for key derivation, quotes, and
    app info (spec §5.1–5.4). ``endpoint`` is the SDK default unix socket
    (``/var/run/dstack.sock``) when None, or a simulator URL for local dev.
    """

    def __init__(self, endpoint: Optional[str] = None):
        self._endpoint = endpoint
        self._client = None

    def _client_or_raise(self):
        if self._client is not None:
            return self._client
        try:
            from dstack_sdk import DstackClient  # lazy: optional dependency
        except Exception as exc:  # depends on deploy env
            raise AttestationUnavailable(f"dstack-sdk unavailable: {exc}") from exc
        self._client = DstackClient(self._endpoint) if self._endpoint else DstackClient()
        return self._client

    def derive_signing_key(self) -> bytes:
        """Ed25519 private key bound to the app identity (spec §5.2)."""
        client = self._client_or_raise()
        resp = client.get_key(SIG_KEY_PATH, "signing")
        if hasattr(resp, "decode_key"):
            return resp.decode_key()[:32]
        return bytes.fromhex(resp.key)[:32]

    def app_info(self) -> AppInfo:
        client = self._client_or_raise()
        info = client.info()
        return AppInfo(
            app_id=getattr(info, "app_id", "") or "",
            instance_id=getattr(info, "instance_id", "") or "",
            compose_hash=getattr(info, "compose_hash", "") or "",
        )

    def quote_and_meta(self, report_data: bytes):
        client = self._client_or_raise()
        q = client.get_quote(report_data)
        # Local-dev sanity check (dstack docs): replay the event log. Never fatal
        # — serving the quote does not depend on the server replaying it; the
        # client does the authoritative §6.2 verification.
        try:  # pragma: no cover - requires a guest agent / simulator
            q.replay_rtmrs()
        except Exception as exc:  # pragma: no cover
            log.warning("replay_rtmrs() sanity check failed: %s", exc)
        info = self.app_info()
        return q.quote, q.event_log, info


class QuoteProvider(Protocol):
    def get_bundle(
        self, identity: SinkIdentity, nonce: Optional[bytes]
    ) -> dict: ...


class DstackQuoteProvider:
    """Real TDX quotes via a :class:`DstackBackend` (only works in a dstack TD,
    or against the simulator). Never fabricates: a missing agent → 503 (§7.3)."""

    def __init__(self, backend: DstackBackend):
        self._backend = backend

    def get_bundle(self, identity: SinkIdentity, nonce: Optional[bytes]) -> dict:
        report_data = compute_report_data(identity.sig_pubkey_bytes, None, nonce)
        try:  # pragma: no cover - requires a real guest agent / simulator
            quote, event_log, info = self._backend.quote_and_meta(report_data)
        except AttestationUnavailable:
            raise
        except Exception as exc:  # pragma: no cover
            raise AttestationUnavailable(f"guest agent error: {exc}") from exc
        return _bundle(
            identity,
            nonce,
            quote=quote,
            event_log=event_log,
            app_id=info.app_id,
            instance_id=info.instance_id,
            compose_hash=info.compose_hash,
        )


class DevQuoteProvider:
    """Fabricated, self-consistent quote for local development (insecure)."""

    def __init__(self, seed: str):
        self._seed = seed
        d = hashlib.sha256(("dev-measure::" + seed).encode()).digest()
        # Deterministic 48-byte SHA-384-shaped register values.
        self._mrtd = hashlib.sha384(b"mrtd" + d).digest()
        self._rtmr = [hashlib.sha384(f"rtmr{i}".encode() + d).digest() for i in range(4)]
        self._compose_hash = hashlib.sha256(b"dev-compose" + d).hexdigest()
        self._app_id = hashlib.sha256(b"dev-app" + d).hexdigest()[:40]
        self._instance_id = hashlib.sha256(b"dev-instance" + d).hexdigest()[:32]

    def get_bundle(self, identity: SinkIdentity, nonce: Optional[bytes]) -> dict:
        report_data = compute_report_data(identity.sig_pubkey_bytes, None, nonce)
        # Quote layout (dev only): magic || report_data || MRTD || RTMR0..3.
        quote = DEV_QUOTE_MAGIC + report_data + self._mrtd + b"".join(self._rtmr)
        event_log = json.dumps(
            [
                {"rtmr": 3, "event": "compose-hash", "digest": self._compose_hash},
                {"rtmr": 3, "event": "app-id", "digest": self._app_id},
                {"rtmr": 3, "event": "instance-id", "digest": self._instance_id},
            ]
        )
        return _bundle(
            identity,
            nonce,
            quote=quote.hex(),
            event_log=event_log,
            app_id=self._app_id,
            instance_id=self._instance_id,
            compose_hash=self._compose_hash,
        )


def _bundle(
    identity: SinkIdentity,
    nonce: Optional[bytes],
    *,
    quote: str,
    event_log: str,
    app_id: str,
    instance_id: str,
    compose_hash: str,
) -> dict:
    return {
        "schema_version": "1",
        "wire": WIRE,
        "quote": quote,
        "event_log": event_log,
        "report_data_construction": {
            "algo": "blake3-512",
            "domain": DOMAIN,
            "fields": ["sink_sig_pubkey", "sink_dh_pubkey", "nonce"],
        },
        "sink_sig_pubkey": identity.sig_pubkey_hex,
        "sink_dh_pubkey": None,
        "nonce": nonce.hex() if nonce is not None else None,
        "app_id": app_id,
        "instance_id": instance_id,
        "compose_hash": compose_hash,
        "kms": {"root_pubkey": None, "signature_chain": []},
        # produced_at is filled by the route (avoids a clock dep in providers).
    }


def build_runtime(settings) -> tuple[SinkIdentity, QuoteProvider, AppInfo]:
    """Wire identity + quote provider + advertised app info from settings.

    dev mode: deterministic seed identity + fabricated quotes.
    dstack mode: derive the signing key from the guest agent (spec §5.2) and
    fetch real app_id/compose_hash. FAILS CLOSED — if the agent is unreachable
    at startup so the key cannot be derived, this raises and the sink refuses to
    boot, rather than fall back to a non-attested seed key. (A later genuine
    quote would otherwise bind that fallback key and a client would pin it.)
    """
    if settings.attest_mode == "dev":
        identity = SinkIdentity.from_seed(settings.dev_seed)
        return identity, DevQuoteProvider(settings.dev_seed), AppInfo(
            app_id=identity.sink_id, compose_hash=""
        )

    backend = DstackBackend(settings.dstack_endpoint)
    # Fail closed (spec §5.2): a dstack sink MUST sign with a guest-agent-derived
    # identity. We refuse to start rather than fall back to a deterministic seed
    # key — a later, genuine quote would otherwise bind that non-attested key and
    # a verifying client would pin it as if it were hardware-derived.
    try:
        identity = SinkIdentity.from_raw(backend.derive_signing_key())
    except Exception as exc:
        raise RuntimeError(
            "dstack mode requires the guest agent to derive sink_sig "
            f"(get_key failed: {exc}). Run inside a dstack TD, set "
            "DSTACK_SIMULATOR_ENDPOINT to the dstack simulator socket, or use "
            "VOXTERM_SINK_ATTEST=dev for local development."
        ) from exc

    try:
        info = backend.app_info()
    except Exception:
        # Identity is attested; app metadata is best-effort and non-fatal.
        info = AppInfo(app_id=identity.sink_id, compose_hash="")
    return identity, DstackQuoteProvider(backend), info
