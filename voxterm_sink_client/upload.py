from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from voxterm_transcript_sink import WIRE

from .http import HTTPResult, HTTPTransport
from .identity import AuthorIdentity
from .transcript import build_transcript
from .verify import SIGNATURE_HEADER, verify_response_signature


@dataclass
class UploadResult:
    path: str
    id: str | None = None
    status: str | None = None
    error: str | None = None

    def public_dict(self) -> dict[str, str]:
        return {key: value for key, value in self.__dict__.items() if value is not None}


def collect_markdown_paths(paths: list[str], recursive: bool = False) -> list[Path]:
    found: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_symlink() and path.is_dir():
            raise ValueError(f"symlinked directories are not supported: {raw}")
        if path.is_dir():
            iterator = path.rglob("*.md") if recursive else path.glob("*.md")
            found.extend(p for p in iterator if p.is_file())
        elif path.is_file():
            found.append(path)
        else:
            raise ValueError(f"path does not exist or is not a file/directory: {raw}")
    return sorted((p.resolve() for p in found), key=lambda p: str(p))


def upload_files(
    paths: list[Path],
    *,
    sink_url: str,
    sink_info: dict[str, Any],
    sink_pubkey: str,
    hivemind_id: str,
    tags: list[str],
    author: AuthorIdentity,
    dry_run: bool,
    transport: HTTPTransport | None = None,
) -> tuple[list[UploadResult], list[UploadResult]]:
    transport = transport or HTTPTransport()
    uploaded: list[UploadResult] = []
    failed: list[UploadResult] = []

    for path in paths:
        try:
            transcript = build_transcript(
                path, sink_info=sink_info, hivemind_id=hivemind_id, tags=tags, author=author
            )
            if dry_run:
                uploaded.append(UploadResult(str(path), transcript["id"], "dry-run"))
                continue
            resp = transport.post_json(
                f"{sink_url}/v1/transcript",
                transcript,
                headers={"X-Sink-Protocol": WIRE, "Content-Type": "application/json"},
            )
            uploaded.append(_handle_upload_response(path, transcript["id"], resp, sink_pubkey))
        except Exception as exc:
            failed.append(UploadResult(str(path), error=str(exc)))

    return uploaded, failed


def _handle_upload_response(
    path: Path, expected_id: str, resp: HTTPResult, sink_pubkey: str
) -> UploadResult:
    try:
        body = resp.json()
    except json.JSONDecodeError:
        raise ValueError(f"HTTP {resp.status}: non-JSON response") from None
    if resp.status in (200, 201):
        if not verify_response_signature(body, resp.headers.get(SIGNATURE_HEADER), sink_pubkey):
            raise ValueError("invalid upload response signature")
        status = "created" if resp.status == 201 else "already_stored"
        returned_id = body.get("id")
        if returned_id != expected_id:
            raise ValueError("upload response id did not match transcript id")
        return UploadResult(str(path), returned_id, status)

    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        err = body["error"]
        code = err.get("code", f"http_{resp.status}")
        message = err.get("message", "")
        raise ValueError(f"{code}: {message}")
    raise ValueError(f"HTTP {resp.status}: {json.dumps(body, sort_keys=True)}")
