"""Pydantic v2 wire models (spec §9, Appendix A.1).

Inbound models set ``extra="allow"`` so that a future client's additive fields
(§9: evolution is additive-only) are preserved through storage and re-serialized
on read. Content addressing and signature verification operate on the RAW
received JSON (see ``canonical.py``), never on a model re-dump, so unknown
fields stay inside the hash.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

ContentType = Literal["transcript", "readout", "summary", "note"]

_ALLOW = ConfigDict(extra="allow")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")  # 32-byte ed25519 / BLAKE3-256, lowercase hex
# VoxTerm session id: datetime.strftime("%Y-%m-%d_%H%M%S") — see VoxTerm tui/app.py.
_SESSION_ID = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")


def _hex64(v: str, name: str) -> str:
    if not isinstance(v, str) or not _HEX64.fullmatch(v):
        raise ValueError(f"{name} must be 64 lowercase hex chars")
    return v


def _session_id(v: str) -> str:
    if not isinstance(v, str) or not _SESSION_ID.fullmatch(v):
        raise ValueError("session_id must match VoxTerm format YYYY-MM-DD_HHMMSS")
    return v


def _is_uuid(v: str) -> str:
    uuid.UUID(v)  # raises ValueError on malformed
    return v


def _rfc3339_utc(v: str) -> str:
    # Spec §1: RFC3339, explicit timezone, UTC. Accept trailing Z or +00:00.
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset() != timezone.utc.utcoffset(dt):
        raise ValueError("timestamp must include an explicit UTC timezone")
    return v


class Speaker(BaseModel):
    model_config = _ALLOW
    local_id: int
    label: Optional[str] = None


class TranscriptChunk(BaseModel):
    model_config = _ALLOW
    schema_version: Literal["1"] = "1"
    sink_id: str
    hivemind_id: str
    session_id: str
    author: str
    seq: int = Field(ge=0)
    created_at: str
    is_final: bool = False
    text: str
    t_start: float
    t_end: float
    speaker: Speaker
    lang: Optional[str] = None
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    revises_seq: Optional[int] = Field(default=None, ge=0)
    tags: list[str] = []
    signature: Optional[str] = None

    @field_validator("author")
    @classmethod
    def _v_author(cls, v: str) -> str:
        return _hex64(v, "author")

    @field_validator("hivemind_id")
    @classmethod
    def _v_hivemind(cls, v: str) -> str:
        return _is_uuid(v)

    @field_validator("session_id")
    @classmethod
    def _v_session(cls, v: str) -> str:
        return _session_id(v)

    @field_validator("created_at")
    @classmethod
    def _v_created(cls, v: str) -> str:
        return _rfc3339_utc(v)


class Segment(BaseModel):
    model_config = _ALLOW
    speaker: Speaker
    text: str
    t_start: float
    t_end: float
    lang: Optional[str] = None
    confidence: Optional[float] = Field(default=None, ge=0, le=1)


class Encryption(BaseModel):
    model_config = _ALLOW
    scheme: Literal["aes-256-gcm"]
    kid: str
    nonce: str
    aad: Optional[str] = None


class Transcript(BaseModel):
    model_config = _ALLOW
    schema_version: Literal["1"] = "1"
    id: str
    sink_id: str
    hivemind_id: str
    session_id: str
    author: str
    content_type: ContentType = "readout"
    created_at: str
    title: Optional[str] = None
    tags: list[str] = []
    parent_ids: list[str] = []
    segments: list[Segment]
    markdown: Optional[str] = None
    source: dict = {}
    encryption: Optional[Encryption] = None
    signature: Optional[str] = None

    @field_validator("id")
    @classmethod
    def _v_id(cls, v: str) -> str:
        return _hex64(v, "id")

    @field_validator("author")
    @classmethod
    def _v_author(cls, v: str) -> str:
        return _hex64(v, "author")

    @field_validator("hivemind_id")
    @classmethod
    def _v_hivemind(cls, v: str) -> str:
        return _is_uuid(v)

    @field_validator("session_id")
    @classmethod
    def _v_session(cls, v: str) -> str:
        return _session_id(v)

    @field_validator("created_at")
    @classmethod
    def _v_created(cls, v: str) -> str:
        return _rfc3339_utc(v)


class StreamHeader(BaseModel):
    model_config = _ALLOW
    schema_version: Literal["1"] = "1"
    type: Literal["stream_header"] = "stream_header"
    sink_id: str
    hivemind_id: str
    session_id: str
    author: str
    started_at: str
    expected_final: bool = False
    client: dict = {}

    @field_validator("author")
    @classmethod
    def _v_author(cls, v: str) -> str:
        return _hex64(v, "author")

    @field_validator("hivemind_id")
    @classmethod
    def _v_hivemind(cls, v: str) -> str:
        return _is_uuid(v)

    @field_validator("session_id")
    @classmethod
    def _v_session(cls, v: str) -> str:
        return _session_id(v)

    @field_validator("started_at")
    @classmethod
    def _v_started(cls, v: str) -> str:
        return _rfc3339_utc(v)


# --- response-shaped models (informational; routes build dicts directly) ----


class StoreResult(BaseModel):
    id: str
    url: str
    stored_at: str


class TranscriptMeta(BaseModel):
    id: str
    session_id: str
    author: str
    content_type: str
    title: Optional[str] = None
    tags: list[str] = []
    created_at: str
    stored_at: str
    bytes: int
    chunk_count: int
    url: str
