"""FastAPI app for the VoxTerm sink (spec §7).

Builds responses manually so data-bearing responses carry ``X-Sink-Signature``
(§7.1): JSON responses are signed over BLAKE3 of their JCS canonicalization
(``canonical_response_body``); NDJSON responses over BLAKE3 of the raw body.
Errors use the §7.1 envelope. All responses carry ``X-Sink-Protocol``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import blake3
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from . import RELEASE, SPEC_VERSION, WIRE
from .assembly import assemble
from .attestation import AppInfo, AttestationUnavailable, QuoteProvider, build_runtime
from .auth import TokenStore
from .canonical import canonical_bytes, content_id, signing_bytes
from .config import Settings
from .identity import SinkIdentity, verify_ed25519
from .models import StreamHeader, Transcript, TranscriptChunk
from .store import ChunkConflict, Store

log = logging.getLogger("voxterm.sink")

PROTOCOL_HEADER = "X-Sink-Protocol"
SIGNATURE_HEADER = "X-Sink-Signature"
SEQ_HEADER = "X-Sink-Seq"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def create_app(
    settings: Optional[Settings] = None,
    *,
    identity: Optional[SinkIdentity] = None,
    provider: Optional[QuoteProvider] = None,
    store: Optional[Store] = None,
    app_info: Optional[AppInfo] = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.warn_insecure()
    # Build identity/provider/info from settings unless fully injected (tests).
    # NB: build_runtime FAILS CLOSED in dstack mode, so injecting both identity
    # and provider is the only way to exercise routes without a guest agent.
    if identity is None or provider is None:
        built_id, built_provider, built_info = build_runtime(settings)
        identity = identity or built_id
        provider = provider or built_provider
        app_info = app_info or built_info
    if app_info is None:
        app_info = AppInfo(app_id=identity.sink_id, compose_hash="")
    store = store or Store(settings.snapshot_path)
    tokens = TokenStore(settings)

    app = FastAPI(title="VoxTerm Sink", version=SPEC_VERSION)

    # --- helpers -----------------------------------------------------

    def _dumps(payload: Any) -> bytes:
        return json.dumps(payload, separators=(",", ":")).encode()

    def signed(payload: Any, status: int = 200) -> Response:
        # Spec §7.1: X-Sink-Signature is over BLAKE3(canonical_response_body).
        # We emit a compact body but sign the JCS canonicalization of it, so a
        # conforming client that recomputes JCS over the parsed JSON verifies.
        body = _dumps(payload)
        digest = blake3.blake3(canonical_bytes(payload)).digest()
        resp = Response(content=body, status_code=status, media_type="application/json")
        resp.headers[SIGNATURE_HEADER] = identity.sign(digest)
        return resp

    def signed_ndjson(body: bytes, extra_headers: dict[str, str] | None = None) -> Response:
        # NDJSON has no single-object canonical form; sign the raw body bytes.
        resp = Response(content=body, media_type="application/x-ndjson")
        resp.headers[SIGNATURE_HEADER] = identity.response_signature(body)
        for k, v in (extra_headers or {}).items():
            resp.headers[k] = v
        return resp

    def err(status: int, code: str, message: str, detail: dict | None = None) -> Response:
        return JSONResponse(
            status_code=status,
            content={"error": {"code": code, "message": message, "detail": detail or {}}},
        )

    def read_authorized(request: Request) -> bool:
        auth = request.headers.get("authorization", "")
        token = auth[len("Bearer ") :] if auth.lower().startswith("bearer ") else None
        return tokens.can_read(token)

    @app.middleware("http")
    async def protocol_headers(request: Request, call_next):
        # §7.1: reject an unknown major version; always advertise ours.
        proto = request.headers.get(PROTOCOL_HEADER)
        if proto and proto != WIRE:
            resp = err(400, "unsupported_protocol", f"this sink speaks {WIRE}")
        else:
            resp = await call_next(request)
        resp.headers[PROTOCOL_HEADER] = WIRE
        return resp

    # --- meta --------------------------------------------------------

    @app.get("/v1/health")
    async def health() -> Response:
        return JSONResponse({"status": "ok"})

    @app.get("/v1/info")
    async def info() -> Response:
        return signed(
            {
                "wire": WIRE,
                "spec_version": SPEC_VERSION,
                "sink_id": identity.sink_id,
                "sink_sig_pubkey": identity.sig_pubkey_hex,
                "sink_dh_pubkey": None,
                "app_id": app_info.app_id,
                "compose_hash": app_info.compose_hash,
                "hiveminds": settings.hiveminds,
                "limits": {
                    # Size caps are enforced (413). rate_per_min is advisory —
                    # enforced at dstack-gateway, not in-app (spec §11.2/§11.8).
                    "max_chunk_bytes": settings.max_chunk_bytes,
                    "max_transcript_bytes": settings.max_transcript_bytes,
                    "rate_per_min": settings.rate_per_min,
                },
                "retention": {"policy": "keep", "ttl_days": None},
                "auth": {
                    "read": "shared-secret-v1",
                    "write": "open-attested-v1",
                    "levels": ["public", "cohort", "coordinator"],
                },
                "governance": {"kms_root_pubkey": None, "allowed_compose_hashes_ref": None},
                "build": {"release": RELEASE, "measurements_ref": None},
            }
        )

    # --- attestation -------------------------------------------------

    @app.get("/v1/attestation")
    async def attestation(request: Request) -> Response:
        nonce_hex = request.query_params.get("nonce")
        nonce: bytes | None = None
        if nonce_hex is not None:
            try:
                nonce = bytes.fromhex(nonce_hex)
            except ValueError:
                return err(400, "bad_request", "nonce must be hex")
            if len(nonce) != 32:
                return err(400, "bad_request", "nonce must be 32 bytes (64 hex chars)")
        try:
            bundle = provider.get_bundle(identity, nonce)
        except AttestationUnavailable as exc:
            return err(503, "attestation_unavailable", str(exc))
        bundle["produced_at"] = _now()
        return signed(bundle)

    # --- auth --------------------------------------------------------

    @app.post("/v1/auth")
    async def auth(request: Request) -> Response:
        try:
            body = json.loads(await request.body())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return err(400, "bad_request", "invalid JSON")
        tier = body.get("tier")
        secret = body.get("secret")
        if tier not in ("cohort", "coordinator") or not isinstance(secret, str):
            return err(400, "bad_request", "tier and secret are required")
        rec = tokens.issue(tier, secret)
        if rec is None:
            return err(401, "unauthorized", "bad secret")
        return JSONResponse(rec)

    # --- writes ------------------------------------------------------

    @app.post("/v1/transcript")
    async def post_transcript(request: Request) -> Response:
        raw_bytes = await request.body()
        if len(raw_bytes) > settings.max_transcript_bytes:
            return err(413, "payload_too_large", "transcript exceeds max_transcript_bytes")
        try:
            raw = json.loads(raw_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return err(400, "bad_request", "invalid JSON")
        try:
            Transcript.model_validate(raw)
        except Exception as exc:
            return err(400, "schema_mismatch", f"invalid transcript: {exc}")

        if content_id(raw) != raw.get("id"):
            return err(409, "id_mismatch", "id does not match content")

        if raw["sink_id"] != identity.sink_id:
            return err(400, "bad_request", "transcript sink_id does not match this sink")

        sig = raw.get("signature")
        if sig is not None and not verify_ed25519(raw["author"], sig, signing_bytes(raw)):
            return err(400, "bad_request", "invalid author signature")

        tid = raw["id"]
        if store.has_transcript(tid):
            return signed({"id": tid, "url": f"/v1/transcript/{tid}", "stored_at": _now()})
        store.put_transcript(tid, raw, _now())
        return signed(
            {"id": tid, "url": f"/v1/transcript/{tid}", "stored_at": _now()}, status=201
        )

    @app.post("/v1/transcript/stream")
    async def post_stream(request: Request) -> Response:
        # PoC note: this buffers the full request body before processing, then
        # returns all acks at once — NOT incremental per-chunk streaming/acking
        # (spec §7.5). Documented as a deliberate cut in docs/DEVELOPMENT.md.
        body = await request.body()
        lines = [ln for ln in body.split(b"\n") if ln.strip()]
        if not lines:
            return err(400, "bad_request", "empty stream")

        try:
            header_raw = json.loads(lines[0])
            StreamHeader.model_validate(header_raw)
        except Exception as exc:
            return err(400, "bad_request", f"first line must be a StreamHeader: {exc}")

        session_id = header_raw["session_id"]
        author = header_raw["author"]
        if header_raw["sink_id"] != identity.sink_id:
            return err(400, "bad_request", "stream sink_id does not match this sink")
        acks: list[dict[str, Any]] = []
        prev_seq = -1  # §7.5: chunk seq is monotonically increasing within a stream

        for line in lines[1:]:
            if len(line) > settings.max_chunk_bytes:
                return err(413, "payload_too_large", "chunk exceeds max_chunk_bytes")
            try:
                chunk_raw = json.loads(line)
                TranscriptChunk.model_validate(chunk_raw)
            except Exception as exc:
                return err(400, "schema_mismatch", f"invalid chunk: {exc}")

            # §7.5: chunks belong to the stream header's declared scope.
            if (
                chunk_raw["session_id"] != session_id
                or chunk_raw["author"] != author
                or chunk_raw["sink_id"] != header_raw["sink_id"]
                or chunk_raw["hivemind_id"] != header_raw["hivemind_id"]
            ):
                return err(
                    400, "bad_request",
                    "chunk sink_id/hivemind_id/session_id/author must match the StreamHeader",
                )
            # §7.5: monotonically increasing seq (starts at 0 for a fresh stream).
            if chunk_raw["seq"] <= prev_seq:
                return err(
                    400, "bad_request",
                    f"non-monotonic seq {chunk_raw['seq']} (previous was {prev_seq})",
                )
            prev_seq = chunk_raw["seq"]

            sig = chunk_raw.get("signature")
            if sig is not None and not verify_ed25519(
                chunk_raw["author"], sig, signing_bytes(chunk_raw)
            ):
                return err(400, "bad_request", f"invalid signature on seq {chunk_raw.get('seq')}")

            try:
                store.append_chunk(chunk_raw, _now())
            except ChunkConflict as exc:
                return err(409, "id_mismatch", f"chunk conflict: {exc}")
            # Durably stored (or already present) → ack stored:true (spec §7.5).
            acks.append({"ack_seq": chunk_raw["seq"], "stored": True})

            if chunk_raw.get("is_final"):
                chunks = store.session_chunks(session_id, author)
                transcript = assemble(session_id, author, chunks)
                store.put_transcript(transcript["id"], transcript, _now())

        body_out = ("\n".join(json.dumps(a) for a in acks) + "\n").encode()
        return signed_ndjson(body_out, {SEQ_HEADER: str(store.max_seq(session_id, author))})

    # --- reads (gated by §8 read tier) -------------------------------

    @app.get("/v1/transcript")
    async def list_transcripts(request: Request) -> Response:
        if not read_authorized(request):
            return err(401, "unauthorized", "read tier requires a bearer token")
        q = request.query_params
        try:
            limit = min(int(q.get("limit", "50")), 500)
        except ValueError:
            return err(400, "bad_request", "limit must be an integer")
        items = store.query_transcripts(
            hivemind_id=q.get("hivemind_id"),
            session_id=q.get("session_id"),
            author=q.get("author"),
            content_type=q.get("content_type"),
            tags=q.getlist("tag") or None,
            since=q.get("since"),
            until=q.get("until"),
            limit=limit,
        )
        # PoC: no cursor pagination.
        return signed({"items": items, "next_cursor": None})

    @app.get("/v1/transcript/{tid}")
    async def get_transcript(tid: str, request: Request) -> Response:
        if not read_authorized(request):
            return err(401, "unauthorized", "read tier requires a bearer token")
        raw = store.get_transcript(tid)
        if raw is None:
            return err(404, "not_found", "no such transcript")
        return signed(raw)

    @app.get("/v1/transcript/{tid}/chunks")
    async def get_chunks(tid: str, request: Request) -> Response:
        if not read_authorized(request):
            return err(401, "unauthorized", "read tier requires a bearer token")
        sess = store.session_for_transcript(tid)
        if sess is None:
            return err(404, "not_found", "no such transcript")
        try:
            since_seq = int(request.query_params.get("since_seq", "-1"))
        except ValueError:
            return err(400, "bad_request", "since_seq must be an integer")
        chunks = [
            c for c in store.session_chunks(*sess) if c.get("seq", -1) > since_seq
        ]
        body_out = ("\n".join(json.dumps(c) for c in chunks) + ("\n" if chunks else "")).encode()
        return signed_ndjson(body_out)

    return app
