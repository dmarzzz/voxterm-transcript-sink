from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import blake3
import pytest

from voxterm_transcript_sink import WIRE
from voxterm_transcript_sink.attestation import DevQuoteProvider, compute_report_data
from voxterm_transcript_sink.canonical import canonical_bytes, content_id, signing_bytes
from voxterm_transcript_sink.identity import SinkIdentity, verify_ed25519
from voxterm_sink_client.http import HTTPResult
from voxterm_sink_client.identity import load_or_create_author
from voxterm_sink_client.transcript import build_transcript, parse_markdown
from voxterm_sink_client.trust import TrustStore
from voxterm_sink_client.upload import collect_markdown_paths, upload_files
from voxterm_sink_client.verify import normalize_sink_url, verify_sink

HIVEMIND = "11111111-1111-1111-1111-111111111111"


def write_export(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def sample_markdown() -> str:
    return """# VoxTerm Transcript

- **Date:** Monday, June 01, 2026
- **Started:** 11:59 PM
- **Model:** qwen3-0.6b
- **Language:** en

---

## Summary

**[23:59:58]** **sys:** loading model
**[23:59:59]** **Alice:** hello
**[00:00:01]** **Bob:** after midnight
**[00:00:02]** unlabelled line
"""


def test_markdown_parser_filters_controls_rollover_and_maps_speakers(tmp_path):
    path = write_export(tmp_path / "2026-06-01_235959-transcript.md", sample_markdown())
    parsed = parse_markdown(path)

    assert parsed.created_at == "2026-06-01T23:59:59Z"
    assert "## Summary" in parsed.markdown
    assert parsed.source_fields["model"] == "qwen3-0.6b"
    assert parsed.source_fields["control_event_count"] == 1
    assert parsed.segments == [
        {
            "speaker": {"local_id": 1, "label": "Alice"},
            "text": "hello",
            "t_start": 0.0,
            "t_end": 2.0,
            "lang": "en",
        },
        {
            "speaker": {"local_id": 2, "label": "Bob"},
            "text": "after midnight",
            "t_start": 2.0,
            "t_end": 3.0,
            "lang": "en",
        },
        {
            "speaker": {"local_id": 0, "label": None},
            "text": "unlabelled line",
            "t_start": 3.0,
            "t_end": 4.0,
            "lang": "en",
        },
    ]


def test_transcript_id_and_author_signature_are_valid(tmp_path):
    path = write_export(tmp_path / "2026-06-01_120000-transcript.md", "**[12:00:01]** hi\n")
    author = load_or_create_author(tmp_path / "author.key")
    transcript = build_transcript(
        path,
        sink_info={"sink_id": "aa" * 20},
        hivemind_id=HIVEMIND,
        tags=["demo"],
        author=author,
    )

    assert transcript["created_at"] == "2026-06-01T12:00:00Z"
    assert transcript["content_type"] == "transcript"
    assert transcript["id"] == content_id(transcript)
    assert verify_ed25519(transcript["author"], transcript["signature"], signing_bytes(transcript))


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("HTTPS://Example.COM:443/v1/", "https://example.com"),
        ("http://Example.COM:80/", "http://example.com"),
        ("https://Example.COM:8443", "https://example.com:8443"),
    ],
)
def test_sink_url_normalization(raw, normalized):
    assert normalize_sink_url(raw) == normalized


def test_directory_scan_is_sorted_and_nonrecursive_by_default(tmp_path):
    write_export(tmp_path / "b.md", "")
    write_export(tmp_path / "a.md", "")
    sub = tmp_path / "sub"
    sub.mkdir()
    write_export(sub / "c.md", "")

    assert [p.name for p in collect_markdown_paths([str(tmp_path)])] == ["a.md", "b.md"]
    assert [p.name for p in collect_markdown_paths([str(tmp_path)], recursive=True)] == [
        "a.md",
        "b.md",
        "c.md",
    ]


class FakeVerifier:
    def verify_attestation(self, bundle: dict[str, Any]) -> dict[str, Any]:
        nonce = bytes.fromhex(bundle["nonce"])
        report = compute_report_data(bytes.fromhex(bundle["sink_sig_pubkey"]), None, nonce).hex()
        return {
            "verified": True,
            "reportdata": report,
            "mrtd": "01" * 48,
            "rtmr0": "02" * 48,
            "rtmr1": "03" * 48,
            "rtmr2": "04" * 48,
            "rtmr3": "05" * 48,
            "app_id": bundle["app_id"],
            "compose_hash": bundle["compose_hash"],
            "instance_id": bundle["instance_id"],
        }


class FakeSinkTransport:
    def __init__(self, identity: SinkIdentity | None = None, *, post_status: int = 201):
        self.identity = identity or SinkIdentity.from_seed("client-test")
        self.provider = DevQuoteProvider("client-test")
        self.post_status = post_status

    def get(self, url: str, headers: dict[str, str] | None = None) -> HTTPResult:
        if "/v1/attestation?nonce=" in url:
            nonce = bytes.fromhex(url.rsplit("nonce=", 1)[1])
            body = self.provider.get_bundle(self.identity, nonce)
            body["produced_at"] = "2026-06-01T00:00:00Z"
            return self._signed_response(200, body)
        if url.endswith("/v1/info"):
            body = {
                "wire": WIRE,
                "spec_version": "1.0.0-draft.1",
                "sink_id": self.identity.sink_id,
                "sink_sig_pubkey": self.identity.sig_pubkey_hex,
                "sink_dh_pubkey": None,
                "app_id": self.provider.get_bundle(self.identity, b"\x00" * 32)["app_id"],
                "compose_hash": self.provider.get_bundle(self.identity, b"\x00" * 32)[
                    "compose_hash"
                ],
            }
            return self._signed_response(200, body)
        raise AssertionError(url)

    def post_json(
        self, url: str, payload: Any, headers: dict[str, str] | None = None
    ) -> HTTPResult:
        if self.post_status in (200, 201):
            body = {"id": payload["id"], "url": f"/v1/transcript/{payload['id']}", "stored_at": "2026-06-01T00:00:00Z"}
            return self._signed_response(self.post_status, body)
        body = {"error": {"code": "bad_request", "message": "nope", "detail": {}}}
        return HTTPResult(self.post_status, {}, json.dumps(body).encode())

    def _signed_response(self, status: int, body: dict[str, Any]) -> HTTPResult:
        raw = json.dumps(body, separators=(",", ":")).encode()
        digest = blake3.blake3(canonical_bytes(body)).digest()
        return HTTPResult(
            status,
            {"X-Sink-Signature": self.identity.sign(digest)},
            raw,
        )


def test_verify_sink_tofu_first_and_repeated_accept(tmp_path):
    trust = TrustStore(tmp_path / "trust.json")
    transport = FakeSinkTransport()

    sink_url, verified, info = verify_sink(
        "HTTP://Example.test:80/v1",
        transport=transport,
        verifier=FakeVerifier(),
        trust_store=trust,
    )
    assert sink_url == "http://example.test"
    assert verified["sink_sig_pubkey"] == transport.identity.sig_pubkey_hex
    assert info["sink_id"] == transport.identity.sink_id

    verify_sink("http://example.test", transport=transport, verifier=FakeVerifier(), trust_store=trust)


def test_verify_sink_tofu_changed_key_rejected(tmp_path):
    trust = TrustStore(tmp_path / "trust.json")
    verify_sink(
        "https://sink.test",
        transport=FakeSinkTransport(SinkIdentity.from_seed("one")),
        verifier=FakeVerifier(),
        trust_store=trust,
    )
    with pytest.raises(ValueError, match="different sink signing key"):
        verify_sink(
            "https://sink.test",
            transport=FakeSinkTransport(SinkIdentity.from_seed("two")),
            verifier=FakeVerifier(),
            trust_store=trust,
        )


def test_trust_reset_removes_last_url_and_sink(tmp_path):
    trust = TrustStore(tmp_path / "trust.json")
    verify_sink(
        "https://sink.test",
        transport=FakeSinkTransport(),
        verifier=FakeVerifier(),
        trust_store=trust,
    )
    assert trust.reset_url("https://sink.test") is True
    assert trust.inspect_public()["sinks"] == {}


def test_upload_success_and_response_signature_verification(tmp_path):
    path = write_export(tmp_path / "2026-06-01_120000-transcript.md", "**[12:00:01]** hi\n")
    trust = TrustStore(tmp_path / "trust.json")
    transport = FakeSinkTransport()
    sink_url, verified, info = verify_sink(
        "https://sink.test", transport=transport, verifier=FakeVerifier(), trust_store=trust
    )
    author = load_or_create_author(tmp_path / "author.key")

    uploaded, failed = upload_files(
        [path],
        sink_url=sink_url,
        sink_info=info,
        sink_pubkey=verified["sink_sig_pubkey"],
        hivemind_id=HIVEMIND,
        tags=[],
        author=author,
        dry_run=False,
        transport=transport,
    )

    assert failed == []
    assert uploaded[0].status == "created"
    assert uploaded[0].id
