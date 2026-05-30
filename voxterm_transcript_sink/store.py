"""In-memory store with optional JSON snapshot (PoC; spec §7 idempotency).

Transcripts are content-addressed and idempotent by ``id``. Chunks are kept in a
fully retained, append-only log keyed by ``(session_id, author)``:

* a byte-identical re-send of an already-seen ``(session_id, author, seq)`` is
  idempotent — acked, not duplicated;
* a *non-identical* chunk for an already-seen ``seq`` is REJECTED
  (:class:`ChunkConflict`). Corrections must use ``revises_seq`` with a new,
  monotonic ``seq`` (spec §9.1) — this keeps one unambiguous chunk per seq.

Durability note (PoC): the primary store is in memory; the optional JSON
snapshot is fsync'd on write but the whole store is rewritten each time. This is
NOT a per-chunk durable write-ahead log — see docs/DEVELOPMENT.md.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

from .canonical import canonical_bytes


class ChunkConflict(Exception):
    """A different chunk already exists for this (session_id, author, seq)."""


def _size(raw: dict[str, Any]) -> int:
    return len(canonical_bytes(raw))


class Store:
    def __init__(self, snapshot_path: Optional[str] = None):
        self._lock = threading.RLock()
        self._snapshot_path = Path(snapshot_path) if snapshot_path else None
        # id -> {"raw": dict, "stored_at": str, "bytes": int}
        self._transcripts: dict[str, dict[str, Any]] = {}
        # (session_id, author) -> list[{"raw": dict, "stored_at": str}]
        self._chunks: dict[tuple[str, str], list[dict[str, Any]]] = {}
        if self._snapshot_path and self._snapshot_path.exists():
            self._load()

    # --- transcripts -------------------------------------------------

    def get_transcript(self, tid: str) -> Optional[dict[str, Any]]:
        with self._lock:
            rec = self._transcripts.get(tid)
            return dict(rec["raw"]) if rec else None

    def has_transcript(self, tid: str) -> bool:
        with self._lock:
            return tid in self._transcripts

    def put_transcript(self, tid: str, raw: dict[str, Any], stored_at: str) -> bool:
        """Store if absent. Returns True if newly created, False if it existed."""
        with self._lock:
            if tid in self._transcripts:
                return False
            self._transcripts[tid] = {
                "raw": dict(raw),
                "stored_at": stored_at,
                "bytes": _size(raw),
            }
            self._save()
            return True

    def query_transcripts(
        self,
        *,
        hivemind_id: Optional[str] = None,
        session_id: Optional[str] = None,
        author: Optional[str] = None,
        content_type: Optional[str] = None,
        tags: Optional[list[str]] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self._lock:
            out: list[dict[str, Any]] = []
            for tid, rec in self._transcripts.items():
                raw = rec["raw"]
                if hivemind_id and raw.get("hivemind_id") != hivemind_id:
                    continue
                if session_id and raw.get("session_id") != session_id:
                    continue
                if author and raw.get("author") != author:
                    continue
                if content_type and raw.get("content_type") != content_type:
                    continue
                if tags and not set(tags).issubset(set(raw.get("tags", []))):
                    continue
                created = raw.get("created_at", "")
                if since and created < since:
                    continue
                if until and created > until:
                    continue
                out.append(self._meta(tid, rec))
            out.sort(key=lambda m: (m["created_at"], m["id"]))
            return out[:limit]

    def _meta(self, tid: str, rec: dict[str, Any]) -> dict[str, Any]:
        raw = rec["raw"]
        key = (raw.get("session_id", ""), raw.get("author", ""))
        chunk_count = len(self._chunks.get(key, []))
        return {
            "id": tid,
            "session_id": raw.get("session_id", ""),
            "author": raw.get("author", ""),
            "content_type": raw.get("content_type", ""),
            "title": raw.get("title"),
            "tags": raw.get("tags", []),
            "created_at": raw.get("created_at", ""),
            "stored_at": rec["stored_at"],
            "bytes": rec["bytes"],
            "chunk_count": chunk_count,
            "url": f"/v1/transcript/{tid}",
        }

    # --- chunks ------------------------------------------------------

    def append_chunk(self, raw: dict[str, Any], stored_at: str) -> bool:
        """Append to the session log. Returns False if a byte-identical chunk for
        the same (session, author, seq) already exists (idempotent). Raises
        :class:`ChunkConflict` if a *different* chunk exists for that seq —
        corrections must use revises_seq with a new seq (spec §9.1)."""
        key = (raw["session_id"], raw["author"])
        with self._lock:
            log = self._chunks.setdefault(key, [])
            incoming = canonical_bytes(raw)
            for entry in log:
                if entry["raw"].get("seq") == raw.get("seq"):
                    if canonical_bytes(entry["raw"]) == incoming:
                        return False  # idempotent re-send
                    raise ChunkConflict(
                        f"seq {raw.get('seq')} already stored with different content"
                    )
            log.append({"raw": dict(raw), "stored_at": stored_at})
            self._save()
            return True

    def session_chunks(self, session_id: str, author: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e["raw"]) for e in self._chunks.get((session_id, author), [])]

    def session_for_transcript(self, tid: str) -> Optional[tuple[str, str]]:
        with self._lock:
            rec = self._transcripts.get(tid)
            if not rec:
                return None
            raw = rec["raw"]
            return raw.get("session_id", ""), raw.get("author", "")

    def max_seq(self, session_id: str, author: str) -> int:
        with self._lock:
            log = self._chunks.get((session_id, author), [])
            return max((e["raw"].get("seq", -1) for e in log), default=-1)

    # --- snapshot ----------------------------------------------------

    def _save(self) -> None:
        if not self._snapshot_path:
            return
        data = {
            "transcripts": self._transcripts,
            "chunks": [
                {"session_id": k[0], "author": k[1], "log": v}
                for k, v in self._chunks.items()
            ],
        }
        tmp = self._snapshot_path.with_suffix(".tmp")
        # Durable atomic replace: fsync the temp file, rename, then fsync the dir.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(data).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self._snapshot_path)
        dir_fd = os.open(self._snapshot_path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _load(self) -> None:
        data = json.loads(self._snapshot_path.read_text())
        self._transcripts = data.get("transcripts", {})
        self._chunks = {
            (e["session_id"], e["author"]): e["log"] for e in data.get("chunks", [])
        }
