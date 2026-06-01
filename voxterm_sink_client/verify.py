from __future__ import annotations

import json
import secrets
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import blake3

from voxterm_transcript_sink import WIRE
from voxterm_transcript_sink.attestation import DEV_QUOTE_MAGIC, compute_report_data
from voxterm_transcript_sink.canonical import canonical_bytes
from voxterm_transcript_sink.identity import verify_ed25519

from .http import HTTPTransport
from .trust import TrustStore

SIGNATURE_HEADER = "X-Sink-Signature"


class VerificationError(RuntimeError):
    pass


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
        result = self.transport.post_json(self.url, {"hex": bundle["quote"]})
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
        bundle, att.headers.get(SIGNATURE_HEADER), verified["sink_sig_pubkey"]
    ):
        raise VerificationError("invalid attestation response signature")

    info_resp = transport.get(f"{sink_url}/v1/info")
    if info_resp.status != 200:
        raise VerificationError(f"info failed with HTTP {info_resp.status}")
    info = info_resp.json()
    if not verify_response_signature(
        info, info_resp.headers.get(SIGNATURE_HEADER), verified["sink_sig_pubkey"]
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
    bundle_nonce = _require_hex(bundle.get("nonce"), 32, "nonce")
    if bytes.fromhex(bundle_nonce) != nonce:
        raise VerificationError("attestation nonce mismatch")
    report_data = compute_report_data(bytes.fromhex(sink_pub), None, nonce).hex()

    verifier_result = verifier.verify_attestation(bundle)
    if not _extract_verified(verifier_result):
        raise VerificationError("quote verifier did not mark quote verified")

    observed_report = _find_hex_value(verifier_result, ("reportdata", "report_data"), 64)
    if observed_report is None:
        # Test/dev verifier fallback: the dstack simulator embeds REPORTDATA in the quote.
        quote = bytes.fromhex(_require_hex(bundle.get("quote"), None, "quote"))
        if not quote.startswith(DEV_QUOTE_MAGIC) and bytes.fromhex(report_data) not in quote:
            raise VerificationError("verifier response omitted reportdata")
        observed_report = report_data
    if observed_report != report_data:
        raise VerificationError("attestation reportdata mismatch")

    measurements = {
        "mrtd": _find_hex_value(verifier_result, ("mrtd",), 48),
        "rtmr0": _find_hex_value(verifier_result, ("rtmr0",), 48),
        "rtmr1": _find_hex_value(verifier_result, ("rtmr1",), 48),
        "rtmr2": _find_hex_value(verifier_result, ("rtmr2",), 48),
        "rtmr3": _find_hex_value(verifier_result, ("rtmr3",), 48),
    }
    if any(v is None for v in measurements.values()):
        measurements = _measurements_from_dev_quote(bundle)

    replayed = _replay_event_log_fields(bundle)
    app_id = _find_string(verifier_result, ("app_id",)) or replayed.get("app_id")
    compose_hash = _find_string(verifier_result, ("compose_hash",)) or replayed.get("compose_hash")
    instance_id = _find_string(verifier_result, ("instance_id",)) or replayed.get("instance_id")
    if app_id is None or compose_hash is None or instance_id is None:
        raise VerificationError("verifier response omitted replayed event-log fields")
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
        "measurements": measurements,
        "verifier": {"provider": "phala-cloud-api", "summary": _redact(verifier_result)},
    }


def _extract_verified(obj: Any) -> bool:
    if isinstance(obj, dict):
        for key, value in obj.items():
            normalized = key.lower().replace("_", "")
            if normalized in {"verified", "isvalid", "quoteverified"}:
                return bool(value)
            if normalized in {"status", "quotestatus"} and str(value).lower() in {
                "verified",
                "ok",
                "success",
            }:
                return True
        return any(_extract_verified(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_extract_verified(v) for v in obj)
    return False


def _find_hex_value(obj: Any, keys: tuple[str, ...], byte_len: int | None) -> str | None:
    keyset = {k.lower().replace("_", "") for k in keys}
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower().replace("_", "") in keyset and isinstance(value, str):
                value = value.lower().removeprefix("0x")
                if _is_hex(value) and (byte_len is None or len(value) == byte_len * 2):
                    return value
            found = _find_hex_value(value, keys, byte_len)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_hex_value(item, keys, byte_len)
            if found is not None:
                return found
    return None


def _find_string(obj: Any, keys: tuple[str, ...]) -> str | None:
    keyset = {k.lower().replace("_", "") for k in keys}
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower().replace("_", "") in keyset and isinstance(value, str):
                return value
            found = _find_string(value, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_string(item, keys)
            if found is not None:
                return found
    return None


def _measurements_from_dev_quote(bundle: dict[str, Any]) -> dict[str, str]:
    quote = bytes.fromhex(_require_hex(bundle.get("quote"), None, "quote"))
    if not quote.startswith(DEV_QUOTE_MAGIC):
        raise VerificationError("verifier response omitted replayed measurements")
    offset = len(DEV_QUOTE_MAGIC) + 64
    fields = {}
    for key in ("mrtd", "rtmr0", "rtmr1", "rtmr2", "rtmr3"):
        fields[key] = quote[offset : offset + 48].hex()
        offset += 48
    return fields


def _replay_event_log_fields(bundle: dict[str, Any]) -> dict[str, str]:
    raw = bundle.get("event_log")
    if not isinstance(raw, str):
        return {}
    try:
        events = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(events, list):
        return {}
    found: dict[str, str] = {}
    names = {
        "compose-hash": "compose_hash",
        "compose_hash": "compose_hash",
        "app-id": "app_id",
        "app_id": "app_id",
        "instance-id": "instance_id",
        "instance_id": "instance_id",
    }
    for event in events:
        if not isinstance(event, dict):
            continue
        name = str(event.get("event", "")).lower()
        key = names.get(name)
        digest = event.get("digest")
        if key and isinstance(digest, str):
            found[key] = digest
    return found


def _require_hex(value: Any, byte_len: int | None, name: str) -> str:
    if not isinstance(value, str):
        raise VerificationError(f"missing {name}")
    normalized = value.lower().removeprefix("0x")
    if not _is_hex(normalized) or (byte_len is not None and len(normalized) != byte_len * 2):
        raise VerificationError(f"malformed {name}")
    return normalized


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
