"""Integration tests for the voxterm-data-sink PoC (spec §5–§9)."""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from voxterm_transcript_sink import WIRE
from voxterm_transcript_sink.app import create_app
from voxterm_transcript_sink.attestation import (
    AttestationUnavailable,
    compute_report_data,
    DEV_QUOTE_MAGIC,
)
from voxterm_transcript_sink.canonical import canonical_bytes, content_id, signing_bytes
from voxterm_transcript_sink.config import Settings
from voxterm_transcript_sink.identity import SinkIdentity, verify_ed25519

SINK_ID = SinkIdentity.from_seed("voxterm-sink-dev-seed").sink_id
HIVEMIND = "11111111-1111-1111-1111-111111111111"
SESSION = "2026-05-30_141503"
AUTHOR = "ab" * 32  # 64-hex placeholder ed25519 pubkey for stream tests


def dev_client() -> TestClient:
    settings = Settings(attest_mode="dev", read_secret="1234")
    return TestClient(create_app(settings))


def author_keypair():
    sk = Ed25519PrivateKey.generate()
    pub = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    return sk, pub


def sink_id(client: TestClient) -> str:
    return client.get("/v1/info").json()["sink_id"]


def make_transcript(
    author: str, *, sink_id: str = SINK_ID, extra: dict | None = None, sk=None
) -> dict:
    raw = {
        "schema_version": "1",
        "sink_id": sink_id,
        "hivemind_id": HIVEMIND,
        "session_id": SESSION,
        "author": author,
        "content_type": "readout",
        "created_at": "2026-05-30T15:02:11Z",
        "tags": ["demo"],
        "parent_ids": [],
        "segments": [
            {"speaker": {"local_id": 1, "label": "Marcus"}, "text": "hello there",
             "t_start": 0.0, "t_end": 1.5}
        ],
    }
    if extra:
        raw.update(extra)
    raw["id"] = content_id(raw)
    if sk is not None:
        raw["signature"] = "ed25519:" + sk.sign(signing_bytes(raw)).hex()
    return raw


def auth_token(client: TestClient, secret: str = "1234") -> str:
    r = client.post("/v1/auth", json={"tier": "cohort", "secret": secret})
    return r.json()["token"]


# --- meta --------------------------------------------------------------


def test_health():
    r = dev_client().get("/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert r.headers["X-Sink-Protocol"] == WIRE


def test_info_shape_and_signature():
    client = dev_client()
    r = client.get("/v1/info")
    assert r.status_code == 200
    body = r.json()
    assert body["wire"] == WIRE
    assert body["limits"]["max_chunk_bytes"] == 64 * 1024
    # Spec §7.1: signature is over BLAKE3(canonical_response_body), i.e. JCS —
    # a conforming client recomputes JCS over the parsed JSON, NOT the raw bytes.
    assert verify_ed25519(
        body["sink_sig_pubkey"],
        r.headers["X-Sink-Signature"],
        _blake3(canonical_bytes(body)),
    )


def _blake3(data: bytes) -> bytes:
    import blake3

    return blake3.blake3(data).digest()


# --- attestation -------------------------------------------------------


def test_attestation_dev_report_data_self_consistent():
    client = dev_client()
    nonce = bytes(range(32))
    r = client.get(f"/v1/attestation?nonce={nonce.hex()}")
    assert r.status_code == 200
    bundle = r.json()
    assert bundle["wire"] == WIRE
    assert bundle["nonce"] == nonce.hex()

    expected_rd = compute_report_data(
        bytes.fromhex(bundle["sink_sig_pubkey"]), None, nonce
    )
    quote = bytes.fromhex(bundle["quote"])
    assert quote.startswith(DEV_QUOTE_MAGIC)
    embedded_rd = quote[len(DEV_QUOTE_MAGIC) : len(DEV_QUOTE_MAGIC) + 64]
    assert embedded_rd == expected_rd  # channel binding (§5.4/§6.2 step 5)


def test_dstack_mode_fails_closed_without_agent(monkeypatch):
    monkeypatch.delenv("DSTACK_SIMULATOR_ENDPOINT", raising=False)
    monkeypatch.delenv("VOXTERM_SINK_DSTACK_ENDPOINT", raising=False)
    # Spec §5.2 fail-closed: with no guest agent,
    # key derivation fails so the sink MUST refuse to start, never fall back
    # to a non-attested seed identity.
    with pytest.raises(RuntimeError):
        create_app(Settings(attest_mode="dstack"))


class _BoomProvider:
    """Quote provider whose agent is up at startup but down at request time."""

    def get_bundle(self, identity, nonce):
        raise AttestationUnavailable("guest agent transiently unreachable")


def test_attestation_runtime_unavailable_returns_503():
    # Identity derived OK at startup (injected), but a later get_quote fails →
    # 503, never a fabricated bundle (§7.3).
    ident = SinkIdentity.from_seed("runtime-503-test")
    app = create_app(Settings(attest_mode="dstack"), identity=ident, provider=_BoomProvider())
    r = TestClient(app).get("/v1/attestation")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "attestation_unavailable"


def test_attestation_bad_nonce():
    r = dev_client().get("/v1/attestation?nonce=zz")
    assert r.status_code == 400


# --- auth --------------------------------------------------------------


def test_auth_good_and_bad_secret():
    client = dev_client()
    assert client.post("/v1/auth", json={"tier": "cohort", "secret": "1234"}).status_code == 200
    bad = client.post("/v1/auth", json={"tier": "cohort", "secret": "nope"})
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "unauthorized"


# --- transcript writes -------------------------------------------------


def test_post_transcript_happy_and_idempotent():
    client = dev_client()
    _, pub = author_keypair()
    t = make_transcript(pub)
    r1 = client.post("/v1/transcript", json=t)
    assert r1.status_code == 201
    assert r1.json()["id"] == t["id"]
    r2 = client.post("/v1/transcript", json=t)  # re-post identical
    assert r2.status_code == 200
    assert r2.json()["id"] == t["id"]


def test_post_transcript_id_mismatch():
    client = dev_client()
    _, pub = author_keypair()
    t = make_transcript(pub)
    t["id"] = "00" * 32  # valid 64-hex shape, but wrong content hash → 409
    r = client.post("/v1/transcript", json=t)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "id_mismatch"


def test_post_transcript_mismatched_sink_rejected():
    client = dev_client()
    _, pub = author_keypair()
    t = make_transcript(pub, sink_id="ff" * 20)
    r = client.post("/v1/transcript", json=t)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_post_transcript_valid_signature_accepted():
    client = dev_client()
    sk, pub = author_keypair()
    t = make_transcript(pub, sk=sk)
    r = client.post("/v1/transcript", json=t)
    assert r.status_code == 201


def test_post_transcript_invalid_signature_rejected():
    client = dev_client()
    sk, pub = author_keypair()
    t = make_transcript(pub, sk=sk)
    t["signature"] = "ed25519:" + "00" * 64  # tamper
    r = client.post("/v1/transcript", json=t)
    assert 400 <= r.status_code < 500
    assert r.status_code != 201


def test_post_transcript_additive_unknown_field_roundtrips():
    client = dev_client()
    _, pub = author_keypair()
    # A future client adds a field; id is computed over the FULL body.
    t = make_transcript(pub, extra={"future_field": {"nested": [1, 2, 3]}})
    r = client.post("/v1/transcript", json=t)
    assert r.status_code == 201
    token = auth_token(client)
    got = client.get(f"/v1/transcript/{t['id']}", headers={"Authorization": f"Bearer {token}"})
    assert got.status_code == 200
    assert got.json()["future_field"] == {"nested": [1, 2, 3]}


def test_post_transcript_too_large():
    client = dev_client()
    settings = Settings(attest_mode="dev", max_transcript_bytes=200)
    client = TestClient(create_app(settings))
    _, pub = author_keypair()
    t = make_transcript(pub, extra={"title": "x" * 500})
    r = client.post("/v1/transcript", json=t)
    assert r.status_code == 413


# --- reads require a token --------------------------------------------


def test_reads_require_token():
    client = dev_client()
    assert client.get("/v1/transcript").status_code == 401
    _, pub = author_keypair()
    t = make_transcript(pub)
    client.post("/v1/transcript", json=t)
    assert client.get(f"/v1/transcript/{t['id']}").status_code == 401


def test_list_query_filters():
    client = dev_client()
    _, pub_a = author_keypair()
    _, pub_b = author_keypair()
    ta = make_transcript(pub_a)
    tb = make_transcript(pub_b, extra={"session_id": "2026-05-30_999999"})
    tb["id"] = content_id({k: v for k, v in tb.items() if k != "id"})
    client.post("/v1/transcript", json=ta)
    client.post("/v1/transcript", json=tb)
    token = auth_token(client)
    hdr = {"Authorization": f"Bearer {token}"}
    r = client.get(f"/v1/transcript?author={pub_a}", headers=hdr)
    assert r.status_code == 200
    ids = [i["id"] for i in r.json()["items"]]
    assert ta["id"] in ids and tb["id"] not in ids


# --- streaming ---------------------------------------------------------


def make_chunk(seq: int, text: str, *, local_id: int = 1, is_final: bool = False,
               revises_seq: int | None = None) -> dict:
    return {
        "schema_version": "1",
        "sink_id": SINK_ID,
        "hivemind_id": HIVEMIND,
        "session_id": SESSION,
        "author": AUTHOR,
        "seq": seq,
        "created_at": f"2026-05-30T14:15:0{seq}.000Z",
        "is_final": is_final,
        "text": text,
        "t_start": float(seq),
        "t_end": float(seq) + 0.9,
        "speaker": {"local_id": local_id, "label": None},
        "revises_seq": revises_seq,
    }


def _ndjson(*objs) -> bytes:
    return ("\n".join(json.dumps(o) for o in objs) + "\n").encode()


def stream_header() -> dict:
    return {
        "schema_version": "1",
        "type": "stream_header",
        "sink_id": SINK_ID,
        "hivemind_id": HIVEMIND,
        "session_id": SESSION,
        "author": AUTHOR,
        "started_at": "2026-05-30T14:15:00Z",
    }


def test_stream_ingest_assemble_and_highwater():
    client = dev_client()
    body = _ndjson(
        stream_header(),
        make_chunk(0, "hello"),
        make_chunk(1, "world", is_final=True),
    )
    r = client.post("/v1/transcript/stream", content=body,
                    headers={"Content-Type": "application/x-ndjson"})
    assert r.status_code == 200
    acks = [json.loads(l) for l in r.text.splitlines()]
    assert acks == [{"ack_seq": 0, "stored": True}, {"ack_seq": 1, "stored": True}]
    assert r.headers["X-Sink-Seq"] == "1"

    # is_final assembled a transcript; find it via list.
    token = auth_token(client)
    hdr = {"Authorization": f"Bearer {token}"}
    items = client.get("/v1/transcript", headers=hdr).json()["items"]
    assert len(items) == 1
    tid = items[0]["id"]
    full = client.get(f"/v1/transcript/{tid}", headers=hdr).json()
    # Same speaker contiguous → one segment "hello world".
    assert len(full["segments"]) == 1
    assert full["segments"][0]["text"] == "hello world"


def test_stream_revision_changes_assembly_but_retains_log():
    client = dev_client()
    body = _ndjson(
        stream_header(),
        make_chunk(0, "helo"),
        make_chunk(1, "world"),
        make_chunk(2, "hello", revises_seq=0, is_final=True),  # correct seq 0
    )
    r = client.post("/v1/transcript/stream", content=body,
                    headers={"Content-Type": "application/x-ndjson"})
    assert r.status_code == 200

    token = auth_token(client)
    hdr = {"Authorization": f"Bearer {token}"}
    tid = client.get("/v1/transcript", headers=hdr).json()["items"][0]["id"]
    full = client.get(f"/v1/transcript/{tid}", headers=hdr).json()
    # seq 0 corrected to "hello"; assembled = "hello world".
    assert full["segments"][0]["text"] == "hello world"

    # /chunks still has all three (append-only retention).
    chunks_resp = client.get(f"/v1/transcript/{tid}/chunks?since_seq=-1", headers=hdr)
    chunks = [json.loads(l) for l in chunks_resp.text.splitlines()]
    assert len(chunks) == 3
    since1 = client.get(f"/v1/transcript/{tid}/chunks?since_seq=1", headers=hdr)
    assert [json.loads(l)["seq"] for l in since1.text.splitlines()] == [2]


def test_stream_invalid_chunk_signature_rejected():
    client = dev_client()
    sk, pub = author_keypair()
    ch = make_chunk(0, "hello", is_final=True)
    ch["author"] = pub
    ch["signature"] = "ed25519:" + "00" * 64  # invalid
    body = _ndjson(stream_header(), ch)
    r = client.post("/v1/transcript/stream", content=body,
                    headers={"Content-Type": "application/x-ndjson"})
    assert 400 <= r.status_code < 500
    assert r.status_code != 200


def test_stream_chunk_mismatched_session_rejected():
    client = dev_client()
    ch = make_chunk(0, "x", is_final=True)
    ch["session_id"] = "2026-01-01_000000"  # != header session
    body = _ndjson(stream_header(), ch)
    r = client.post("/v1/transcript/stream", content=body,
                    headers={"Content-Type": "application/x-ndjson"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_stream_chunk_mismatched_sink_or_hivemind_rejected():
    client = dev_client()

    bad_sink = make_chunk(0, "x", is_final=True)
    bad_sink["sink_id"] = "ff" * 20
    r = client.post("/v1/transcript/stream", content=_ndjson(stream_header(), bad_sink),
                    headers={"Content-Type": "application/x-ndjson"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"

    bad_hivemind = make_chunk(0, "x", is_final=True)
    bad_hivemind["hivemind_id"] = "22222222-2222-2222-2222-222222222222"
    r2 = client.post("/v1/transcript/stream",
                     content=_ndjson(stream_header(), bad_hivemind),
                     headers={"Content-Type": "application/x-ndjson"})
    assert r2.status_code == 400
    assert r2.json()["error"]["code"] == "bad_request"


def test_stream_header_mismatched_sink_rejected():
    client = dev_client()
    header = stream_header()
    header["sink_id"] = "ff" * 20
    ch = make_chunk(0, "x", is_final=True)
    ch["sink_id"] = "ff" * 20
    r = client.post("/v1/transcript/stream", content=_ndjson(header, ch),
                    headers={"Content-Type": "application/x-ndjson"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_stream_non_monotonic_seq_rejected():
    client = dev_client()
    body = _ndjson(stream_header(), make_chunk(0, "a"), make_chunk(0, "b"))
    r = client.post("/v1/transcript/stream", content=body,
                    headers={"Content-Type": "application/x-ndjson"})
    assert r.status_code == 400
    assert "monotonic" in r.json()["error"]["message"]


def test_bad_query_params_return_envelope_not_500():
    client = dev_client()
    token = auth_token(client)
    hdr = {"Authorization": f"Bearer {token}"}
    r = client.get("/v1/transcript?limit=abc", headers=hdr)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"
    # Seed a transcript so the chunks route resolves a session, then bad since_seq.
    _, pub = author_keypair()
    t = make_transcript(pub)
    client.post("/v1/transcript", json=t)
    r2 = client.get(f"/v1/transcript/{t['id']}/chunks?since_seq=xyz", headers=hdr)
    assert r2.status_code == 400


def test_stream_conflicting_same_seq_rejected():
    # A different chunk for an already-stored (session, author, seq) → 409;
    # corrections must use revises_seq with a new seq (spec §9.1).
    client = dev_client()
    body1 = _ndjson(stream_header(), make_chunk(0, "first"))
    assert client.post("/v1/transcript/stream", content=body1,
                       headers={"Content-Type": "application/x-ndjson"}).status_code == 200
    # Reconnect: same seq 0, different text.
    body2 = _ndjson(stream_header(), make_chunk(0, "different"))
    r = client.post("/v1/transcript/stream", content=body2,
                    headers={"Content-Type": "application/x-ndjson"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "id_mismatch"
    # Identical re-send is still idempotent (200).
    r2 = client.post("/v1/transcript/stream", content=body1,
                     headers={"Content-Type": "application/x-ndjson"})
    assert r2.status_code == 200


def test_invalid_field_values_rejected():
    client = dev_client()
    _, pub = author_keypair()
    # Bad author hex.
    bad_author = make_transcript("nothex")
    assert client.post("/v1/transcript", json=bad_author).status_code == 400
    # confidence out of range.
    t = make_transcript(pub)
    t["segments"][0]["confidence"] = 1.5
    t["id"] = content_id(t)
    assert client.post("/v1/transcript", json=t).status_code == 400
    # Non-UUID hivemind_id.
    t2 = make_transcript(pub, extra={"hivemind_id": "not-a-uuid"})
    assert client.post("/v1/transcript", json=t2).status_code == 400
    # session_id not in VoxTerm YYYY-MM-DD_HHMMSS shape.
    t3 = make_transcript(pub, extra={"session_id": "2026/05/30 14:15"})
    assert client.post("/v1/transcript", json=t3).status_code == 400
    # timestamps must be explicit UTC.
    t4 = make_transcript(pub, extra={"created_at": "2026-05-30T15:02:11+05:00"})
    assert client.post("/v1/transcript", json=t4).status_code == 400
    t5 = make_transcript(pub, extra={"created_at": "2026-05-30T15:02:11"})
    assert client.post("/v1/transcript", json=t5).status_code == 400


def test_unsupported_protocol_header():
    client = dev_client()
    r = client.get("/v1/health", headers={"X-Sink-Protocol": "voxterm-sink/99"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "unsupported_protocol"
