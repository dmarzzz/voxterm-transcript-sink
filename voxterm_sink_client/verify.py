from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from hashlib import sha384
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import blake3

from voxterm_transcript_sink import WIRE
from voxterm_transcript_sink.attestation import compute_report_data
from voxterm_transcript_sink.canonical import canonical_bytes
from voxterm_transcript_sink.identity import verify_ed25519

from .http import HTTPTransport
from .trust import TrustStore

SIGNATURE_HEADER = "X-Sink-Signature"
SIGNATURE_HEADER_KEY = SIGNATURE_HEADER.lower()


class VerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedQuote:
    reportdata: str
    measurements: dict[str, str]


def normalize_sink_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("sink URL must be absolute http(s)")
    if parsed.query or parsed.fragment:
        raise ValueError("sink URL must not include query or fragment")
    path = parsed.path.rstrip("/")
    if path == "":
        path = "/"
    if path not in {"/", "/v1"}:
        raise ValueError("sink URL path must be / or /v1")
    host = parsed.hostname.lower()
    port = parsed.port
    netloc = host
    if port and not (
        (parsed.scheme.lower() == "https" and port == 443)
        or (parsed.scheme.lower() == "http" and port == 80)
    ):
        netloc = f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))


def verify_response_signature(body: dict[str, Any], signature: str | None, pubkey: str) -> bool:
    if not signature:
        return False
    digest = blake3.blake3(canonical_bytes(body)).digest()
    return verify_ed25519(pubkey, signature, digest)


class PhalaCloudVerifier:
    url = "https://cloud-api.phala.com/api/v1/attestations/verify"

    def __init__(self, transport: HTTPTransport | None = None):
        self.transport = transport or HTTPTransport()

    def verify_attestation(self, bundle: dict[str, Any]) -> dict[str, Any]:
        quote = _require_hex(bundle.get("quote"), None, "quote")
        result = self.transport.post_json(self.url, {"hex": quote})
        if result.status < 200 or result.status >= 300:
            raise VerificationError(f"Phala verifier returned HTTP {result.status}")
        return result.json()


def verify_sink(
    sink_url: str,
    *,
    transport: HTTPTransport | None = None,
    verifier: Any | None = None,
    trust_store: TrustStore | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    sink_url = normalize_sink_url(sink_url)
    transport = transport or HTTPTransport()
    verifier = verifier or PhalaCloudVerifier()
    trust_store = trust_store or TrustStore()

    nonce = secrets.token_bytes(32)
    att = transport.get(f"{sink_url}/v1/attestation?nonce={nonce.hex()}")
    if att.status != 200:
        raise VerificationError(f"attestation failed with HTTP {att.status}")
    try:
        bundle = att.json()
    except json.JSONDecodeError as exc:
        raise VerificationError("attestation response was not valid JSON") from exc

    verified = _verify_attestation_bundle(bundle, nonce, verifier)
    if not verify_response_signature(
        bundle, att.headers.get(SIGNATURE_HEADER_KEY), verified["sink_sig_pubkey"]
    ):
        raise VerificationError("invalid attestation response signature")

    info_resp = transport.get(f"{sink_url}/v1/info")
    if info_resp.status != 200:
        raise VerificationError(f"info failed with HTTP {info_resp.status}")
    info = info_resp.json()
    if not verify_response_signature(
        info, info_resp.headers.get(SIGNATURE_HEADER_KEY), verified["sink_sig_pubkey"]
    ):
        raise VerificationError("invalid info response signature")
    if info.get("sink_sig_pubkey") != verified["sink_sig_pubkey"]:
        raise VerificationError("info sink key does not match attestation")
    if info.get("app_id", "") != verified.get("app_id", ""):
        raise VerificationError("info app_id does not match attestation")
    if info.get("compose_hash", "") != verified.get("compose_hash", ""):
        raise VerificationError("info compose_hash does not match attestation")

    trust_store.apply(sink_url, verified)
    return sink_url, verified, info


def _verify_attestation_bundle(
    bundle: dict[str, Any], nonce: bytes, verifier: Any
) -> dict[str, Any]:
    sink_pub = _require_hex(bundle.get("sink_sig_pubkey"), 32, "sink_sig_pubkey")
    dh_pub = _optional_hex(bundle.get("sink_dh_pubkey"), 32, "sink_dh_pubkey")
    bundle_nonce = _require_hex(bundle.get("nonce"), 32, "nonce")
    if bytes.fromhex(bundle_nonce) != nonce:
        raise VerificationError("attestation nonce mismatch")
    report_data = compute_report_data(
        bytes.fromhex(sink_pub),
        bytes.fromhex(dh_pub) if dh_pub is not None else None,
        nonce,
    ).hex()

    verifier_result = verifier.verify_attestation(bundle)
    quote = _extract_verified_quote(verifier_result)
    if quote.reportdata != report_data:
        raise VerificationError("attestation reportdata mismatch")

    replayed = _replay_event_log(bundle)
    if replayed["rtmr3"] != quote.measurements["rtmr3"]:
        raise VerificationError("event log RTMR3 replay mismatch")
    app_id = replayed["app_id"]
    compose_hash = replayed["compose_hash"]
    instance_id = replayed["instance_id"]
    if app_id != bundle.get("app_id", ""):
        raise VerificationError("attestation app_id mismatch")
    if compose_hash != bundle.get("compose_hash", ""):
        raise VerificationError("attestation compose_hash mismatch")
    if instance_id != bundle.get("instance_id", ""):
        raise VerificationError("attestation instance_id mismatch")

    return {
        "sink_sig_pubkey": sink_pub,
        "app_id": app_id,
        "compose_hash": compose_hash,
        "instance_id": instance_id,
        "measurements": quote.measurements,
        "verifier": {"provider": "phala-cloud-api", "summary": _redact(verifier_result)},
    }


def _extract_verified_quote(verifier_result: dict[str, Any]) -> VerifiedQuote:
    if not isinstance(verifier_result, dict) or verifier_result.get("success") is not True:
        raise VerificationError("quote verifier did not mark request successful")
    quote = verifier_result.get("quote")
    if not isinstance(quote, dict) or quote.get("verified") is not True:
        raise VerificationError("quote verifier did not mark quote verified")
    body = quote.get("body")
    if not isinstance(body, dict):
        raise VerificationError("verifier response omitted quote body")
    measurements = {
        "mrtd": _require_hex(body.get("mrtd"), 48, "mrtd"),
        "rtmr0": _require_hex(body.get("rtmr0"), 48, "rtmr0"),
        "rtmr1": _require_hex(body.get("rtmr1"), 48, "rtmr1"),
        "rtmr2": _require_hex(body.get("rtmr2"), 48, "rtmr2"),
        "rtmr3": _require_hex(body.get("rtmr3"), 48, "rtmr3"),
    }
    return VerifiedQuote(
        reportdata=_require_hex(body.get("reportdata"), 64, "reportdata"),
        measurements=measurements,
    )


def _replay_event_log(bundle: dict[str, Any]) -> dict[str, str]:
    raw = bundle.get("event_log")
    if not isinstance(raw, str):
        raise VerificationError("attestation event_log is missing")
    try:
        events = json.loads(raw)
    except json.JSONDecodeError:
        raise VerificationError("attestation event_log is malformed JSON") from None
    if not isinstance(events, list):
        raise VerificationError("attestation event_log must be a JSON array")
    found: dict[str, str] = {}
    names = {
        "compose-hash": "compose_hash",
        "compose_hash": "compose_hash",
        "app-id": "app_id",
        "app_id": "app_id",
        "instance-id": "instance_id",
        "instance_id": "instance_id",
    }
    history: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        imr = event.get("imr", event.get("rtmr"))
        if str(imr) != "3":
            continue
        digest = _event_digest(event)
        history.append(digest)
        name = str(event.get("event", "")).lower()
        key = names.get(name)
        if key:
            found[key] = _metadata_event_value(event, key)
    missing = [key for key in ("app_id", "compose_hash", "instance_id") if key not in found]
    if missing:
        raise VerificationError(f"attestation event_log missing {', '.join(missing)}")
    if not history:
        raise VerificationError("attestation event_log has no RTMR3 events")
    return {**found, "rtmr3": _replay_rtmr(history)}


def _event_digest(event: dict[str, Any]) -> str:
    digest = event.get("digest")
    payload = event.get("event_payload")
    if isinstance(digest, str):
        normalized = _require_hex(digest, 48, "event digest")
        if isinstance(payload, str):
            if normalized not in _payload_digest_candidates(payload):
                raise VerificationError("RTMR3 event digest does not match event_payload")
        return normalized
    if isinstance(payload, str):
        return _payload_digest_candidates(payload)[0]
    raise VerificationError("RTMR3 event missing digest or event_payload")


def _metadata_event_value(event: dict[str, Any], key: str) -> str:
    value = _event_payload_value(event)
    if not value:
        raise VerificationError(f"attestation event_log {key} event missing event_payload")
    return value


def _event_payload_value(event: dict[str, Any]) -> str | None:
    payload = event.get("event_payload")
    if not isinstance(payload, str) or not payload:
        return None
    payload = _decode_event_payload(payload)
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return payload
    if isinstance(decoded, str):
        return decoded
    if isinstance(decoded, dict):
        for key in ("digest", "value", "id", "hash"):
            value = decoded.get(key)
            if isinstance(value, str):
                return value
    return None


def _payload_digest_candidates(payload: str) -> list[str]:
    candidates = [sha384(payload.encode("utf-8")).hexdigest()]
    try:
        raw = bytes.fromhex(payload)
    except ValueError:
        return candidates
    if len(payload) % 2 == 0:
        candidates.insert(0, sha384(raw).hexdigest())
    return candidates


def _decode_event_payload(payload: str) -> str:
    if len(payload) % 2 != 0:
        return payload
    try:
        decoded = bytes.fromhex(payload).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return payload
    return decoded


def _replay_rtmr(history: list[str]) -> str:
    mr = b"\x00" * 48
    for digest_hex in history:
        digest = bytes.fromhex(digest_hex)
        if len(digest) < 48:
            digest = digest.ljust(48, b"\0")
        mr = sha384(mr + digest).digest()
    return mr.hex()


def _require_hex(value: Any, byte_len: int | None, name: str) -> str:
    if not isinstance(value, str):
        raise VerificationError(f"missing {name}")
    normalized = value.lower().removeprefix("0x")
    if not _is_hex(normalized) or (byte_len is not None and len(normalized) != byte_len * 2):
        raise VerificationError(f"malformed {name}")
    return normalized


def _optional_hex(value: Any, byte_len: int, name: str) -> str | None:
    if value is None:
        return None
    return _require_hex(value, byte_len, name)


def _is_hex(value: str) -> bool:
    try:
        bytes.fromhex(value)
        return len(value) % 2 == 0
    except ValueError:
        return False


def _redact(obj: Any) -> Any:
    # The verifier response should not contain secrets, but keep the trust store compact.
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items() if k.lower() not in {"quote"}}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj
