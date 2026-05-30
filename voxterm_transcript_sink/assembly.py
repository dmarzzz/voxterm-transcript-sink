"""Deterministic server-side assembly of a Transcript from chunks (spec §9.4).

Order retained chunks by effective ``seq``, applying ``revises_seq`` (a chunk
revising seq K supersedes K's content; latest wins per §9.1/§9.4), group
contiguous same-speaker chunks into segments, render simple markdown, and
content-address the result. Deterministic so the assembled ``id`` is reproducible.
"""

from __future__ import annotations

from typing import Any

from . import SCHEMA_VERSION
from .canonical import content_id


def assemble(session_id: str, author: str, chunks: list[dict[str, Any]]) -> dict[str, Any]:
    if not chunks:
        raise ValueError("cannot assemble an empty chunk log")

    # Resolve each effective seq to its latest chunk (revises_seq redirects).
    effective: dict[int, dict[str, Any]] = {}
    for c in chunks:
        target = c["revises_seq"] if c.get("revises_seq") is not None else c["seq"]
        effective[target] = c

    ordered = [effective[s] for s in sorted(effective)]

    segments: list[dict[str, Any]] = []
    for c in ordered:
        sp = c["speaker"]
        if segments and segments[-1]["speaker"].get("local_id") == sp.get("local_id"):
            seg = segments[-1]
            seg["text"] = (seg["text"] + " " + c["text"]).strip()
            seg["t_end"] = c["t_end"]
            if c.get("confidence") is not None:
                seg["confidence"] = c["confidence"]
        else:
            seg = {
                "speaker": {"local_id": sp.get("local_id"), "label": sp.get("label")},
                "text": c["text"],
                "t_start": c["t_start"],
                "t_end": c["t_end"],
            }
            if c.get("lang") is not None:
                seg["lang"] = c["lang"]
            if c.get("confidence") is not None:
                seg["confidence"] = c["confidence"]
            segments.append(seg)

    first = ordered[0]
    final = next((c for c in reversed(chunks) if c.get("is_final")), chunks[-1])

    def _name(sp: dict[str, Any]) -> str:
        return sp.get("label") or f"Speaker {sp.get('local_id')}"

    markdown = "\n\n".join(
        f"**{_name(s['speaker'])}:** {s['text']}" for s in segments
    )

    transcript: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "sink_id": first["sink_id"],
        "hivemind_id": first["hivemind_id"],
        "session_id": session_id,
        "author": author,
        "content_type": "transcript",
        "created_at": final.get("created_at", first.get("created_at")),
        "title": None,
        "tags": [],
        "parent_ids": [],
        "segments": segments,
        "markdown": markdown,
        "source": {
            "tool": "voxterm-data-sink",
            "tool_version": "0.1.0",
            "stream_chunk_count": len(chunks),
            "finalized": True,
        },
        "encryption": None,
    }
    transcript["id"] = content_id(transcript)
    return transcript
