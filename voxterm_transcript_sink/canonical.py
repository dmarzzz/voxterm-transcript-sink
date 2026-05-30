"""Canonicalization, content addressing, and signature checks (spec §1, §9.5).

Canonical bytes are produced by JCS (RFC 8785) over the raw parsed JSON object
with the ``id`` and ``signature`` fields removed. We deliberately operate on the
RAW object (``json.loads(body)``) rather than a Pydantic re-dump: a future
client's additive fields (§9) must remain inside the hash so its ``id`` and
``signature`` still recompute correctly on an older sink.
"""

from __future__ import annotations

from typing import Any, Iterable

import blake3
import rfc8785


def canonical_bytes(obj: dict[str, Any], drop: Iterable[str] = ()) -> bytes:
    """JCS canonical bytes of ``obj`` with ``drop`` keys removed."""
    dropped = set(drop)
    pruned = {k: v for k, v in obj.items() if k not in dropped}
    return rfc8785.dumps(pruned)


def content_id(obj: dict[str, Any]) -> str:
    """BLAKE3-256 hex over ``JCS(obj \\ {id, signature})`` (spec §9.2)."""
    return blake3.blake3(canonical_bytes(obj, drop=("id", "signature"))).hexdigest()


def signing_bytes(obj: dict[str, Any]) -> bytes:
    """Canonical bytes a signature covers: ``JCS(obj \\ {signature})`` (§9.1/§9.2)."""
    return canonical_bytes(obj, drop=("signature",))
