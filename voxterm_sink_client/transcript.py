from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import blake3

from voxterm_transcript_sink.canonical import content_id, signing_bytes

from . import SPEC_VERSION, TOOL_NAME
from .identity import AuthorIdentity

SESSION_RE = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{6})")
LINE_RE = re.compile(
    r"^\*\*\[(?P<time>\d{2}:\d{2}:\d{2})\]\*\*\s+(?:(?:\*\*(?P<label>(?:(?!:\*\*).)+):\*\*)\s+)?(?P<text>.*)$"
)
HEADER_RE = re.compile(r"^-\s+\*\*(?P<key>Model|Language):\*\*\s*(?P<value>.+?)\s*$", re.I)
CONTROL_LABELS = {"sys", "rec", "party"}


@dataclass(frozen=True)
class ParsedMarkdown:
    session_id: str
    created_at: str
    title: str
    markdown: str
    segments: list[dict[str, Any]]
    source_fields: dict[str, Any]


def package_version() -> str:
    try:
        return version("voxterm-data-sink")
    except PackageNotFoundError:
        return "0.1.0"


def parse_markdown(path: Path) -> ParsedMarkdown:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    match = SESSION_RE.search(path.name)
    if not match:
        raise ValueError(f"{path}: filename must contain YYYY-MM-DD_HHMMSS")
    session_id = match.group(1)
    session_dt = datetime.strptime(session_id, "%Y-%m-%d_%H%M%S").replace(
        tzinfo=timezone.utc
    )
    start_sod = (
        session_dt.hour * 3600 + session_dt.minute * 60 + session_dt.second
    )

    model: str | None = None
    language: str | None = None
    speaker_ids: dict[str, int] = {}
    raw_segments: list[tuple[int, str | None, str]] = []
    control_count = 0
    previous_clock: int | None = None
    day_offset = 0

    for line in text.splitlines():
        header = HEADER_RE.match(line)
        if header:
            key = header.group("key").lower()
            if key == "model":
                model = header.group("value")
            elif key == "language":
                language = header.group("value")

        m = LINE_RE.match(line)
        if not m:
            continue
        clock = _clock_seconds(m.group("time"))
        if previous_clock is not None and clock < previous_clock:
            day_offset += 24 * 3600
        previous_clock = clock
        label = m.group("label")
        label = label.strip() if label is not None else None
        body = m.group("text").strip()
        if label is not None and label.lower() in CONTROL_LABELS:
            control_count += 1
            continue
        raw_segments.append((day_offset + clock, label, body))

    if not raw_segments:
        raise ValueError(f"{path}: no speech transcript lines found")

    segments: list[dict[str, Any]] = []
    for idx, (absolute_seconds, label, body) in enumerate(raw_segments):
        t_start = absolute_seconds - start_sod
        if t_start < 0:
            if t_start >= -300:
                t_start = 0
            else:
                raise ValueError(f"{path}: transcript line precedes session start")
        if idx + 1 < len(raw_segments):
            t_end = raw_segments[idx + 1][0] - start_sod
            if t_end < 0 and t_end >= -300:
                t_end = 0
        else:
            t_end = t_start + 1.0

        if label is None:
            speaker = {"local_id": 0, "label": None}
        else:
            if label not in speaker_ids:
                speaker_ids[label] = len(speaker_ids) + 1
            speaker = {"local_id": speaker_ids[label], "label": label}
        seg: dict[str, Any] = {
            "speaker": speaker,
            "text": body,
            "t_start": round(float(t_start), 3),
            "t_end": round(float(t_end), 3),
        }
        if language:
            seg["lang"] = language
        segments.append(seg)

    source: dict[str, Any] = {
        "tool": TOOL_NAME,
        "tool_version": package_version(),
        "spec_version": SPEC_VERSION,
        "input_format": "voxterm-markdown",
        "filename": path.name,
        "file_blake3": blake3.blake3(text.encode("utf-8")).hexdigest(),
    }
    if model:
        source["model"] = model
    if language:
        source["language"] = language
    if control_count:
        source["control_event_count"] = control_count

    return ParsedMarkdown(
        session_id=session_id,
        created_at=session_dt.isoformat().replace("+00:00", "Z"),
        title=path.stem,
        markdown=text,
        segments=segments,
        source_fields=source,
    )


def build_transcript(
    path: Path,
    *,
    sink_info: dict[str, Any],
    hivemind_id: str,
    tags: list[str],
    author: AuthorIdentity,
) -> dict[str, Any]:
    parsed = parse_markdown(path)
    raw: dict[str, Any] = {
        "schema_version": "1",
        "sink_id": sink_info["sink_id"],
        "hivemind_id": hivemind_id,
        "session_id": parsed.session_id,
        "author": author.public_hex,
        "content_type": "transcript",
        "created_at": parsed.created_at,
        "title": parsed.title,
        "tags": tags,
        "parent_ids": [],
        "segments": parsed.segments,
        "markdown": parsed.markdown,
        "source": parsed.source_fields,
    }
    raw["id"] = content_id(raw)
    raw["signature"] = author.sign(signing_bytes(raw))
    return raw


def _clock_seconds(value: str) -> int:
    hour, minute, second = (int(part) for part in value.split(":"))
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError(f"invalid transcript timestamp {value}")
    return hour * 3600 + minute * 60 + second
