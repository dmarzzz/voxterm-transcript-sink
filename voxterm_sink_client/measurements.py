"""Pinned-measurement policy for sink verification (spec §6.3).

TOFU (the client default) records whatever measurements a sink first presents and
warns if they later change. PINNED instead checks the live quote against a
*known-good* release manifest (`measurements.json`, spec Appendix B): the
RTMR3 compose-hash must equal the pinned `compose_hash`, and the
MRTD/RTMR0..2 base-image registers must match one pinned `dstack_base_images`
entry. A mismatch fails closed.

RTMR3 itself is not pinned here: `verify._verify_attestation_bundle` already
binds it to `compose_hash` via event-log replay, so pinning `compose_hash`
covers the app-config layer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

POLICY_TOFU = "tofu"
POLICY_PINNED = "pinned"
POLICIES = (POLICY_TOFU, POLICY_PINNED)

# Filename shipped inside the client package and published at the repo root.
MEASUREMENTS_FILENAME = "measurements.json"

# The four base-image registers a pinned client matches against a known release.
# RTMR3 is intentionally excluded (it is the per-deployment compose-hash layer,
# checked separately via `compose_hash`).
_BASE_REGISTERS = ("mrtd", "rtmr0", "rtmr1", "rtmr2")


class MeasurementError(ValueError):
    """Raised when a manifest is malformed or a quote fails pinned verification."""


def _norm_hex(value: Any, byte_len: int | None, field: str) -> str:
    if not isinstance(value, str):
        raise MeasurementError(f"{field} must be a hex string")
    normalized = value.strip().lower().removeprefix("0x")
    if normalized.startswith("<fill") or normalized.startswith("<"):
        raise MeasurementError(f"{field} is an unfilled placeholder")
    try:
        raw = bytes.fromhex(normalized)
    except ValueError:
        raise MeasurementError(f"{field} is not valid hex") from None
    if byte_len is not None and len(raw) != byte_len:
        raise MeasurementError(f"{field} must be {byte_len} bytes ({byte_len * 2} hex chars)")
    return normalized


@dataclass(frozen=True)
class BaseImage:
    """One known-good dstack base image: its MRTD + RTMR0..2 (hex sha384)."""

    name: str
    mrtd: str
    rtmr0: str
    rtmr1: str
    rtmr2: str

    @classmethod
    def from_dict(cls, data: Any, *, index: int) -> "BaseImage":
        if not isinstance(data, dict):
            raise MeasurementError(f"dstack_base_images[{index}] must be an object")
        name = data.get("name")
        if not isinstance(name, str) or not name or name.startswith("<"):
            raise MeasurementError(f"dstack_base_images[{index}].name is missing")
        return cls(
            name=name,
            mrtd=_norm_hex(data.get("mrtd"), 48, f"dstack_base_images[{index}].mrtd"),
            rtmr0=_norm_hex(data.get("rtmr0"), 48, f"dstack_base_images[{index}].rtmr0"),
            rtmr1=_norm_hex(data.get("rtmr1"), 48, f"dstack_base_images[{index}].rtmr1"),
            rtmr2=_norm_hex(data.get("rtmr2"), 48, f"dstack_base_images[{index}].rtmr2"),
        )

    def registers(self) -> dict[str, str]:
        return {"mrtd": self.mrtd, "rtmr0": self.rtmr0, "rtmr1": self.rtmr1, "rtmr2": self.rtmr2}


@dataclass(frozen=True)
class ReleaseMeasurements:
    """A validated, fully-filled `measurements.json` ready for pinned checks."""

    release: str
    compose_hash: str
    base_images: tuple[BaseImage, ...]
    image: str | None = None

    @classmethod
    def from_dict(cls, data: Any) -> "ReleaseMeasurements":
        if not isinstance(data, dict):
            raise MeasurementError("measurements manifest must be a JSON object")
        base_raw = data.get("dstack_base_images")
        if not isinstance(base_raw, list) or not base_raw:
            raise MeasurementError("measurements manifest has no dstack_base_images")
        base_images = tuple(
            BaseImage.from_dict(entry, index=i) for i, entry in enumerate(base_raw)
        )
        image = data.get("image")
        if isinstance(image, str) and image.startswith("<"):
            image = None
        return cls(
            release=str(data.get("release", "")),
            compose_hash=_norm_hex(data.get("compose_hash"), 32, "compose_hash"),
            base_images=base_images,
            image=image if isinstance(image, str) else None,
        )

    @classmethod
    def load(cls, path: str | Path) -> "ReleaseMeasurements":
        path = Path(path)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise MeasurementError(f"cannot read measurements file {path}: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MeasurementError(f"measurements file {path} is not valid JSON") from exc
        return cls.from_dict(data)

    def check(self, *, compose_hash: str, measurements: dict[str, str]) -> BaseImage:
        """Verify a live quote against this release. Returns the matched base image.

        Raises MeasurementError (fail closed) on any mismatch.
        """
        observed_compose = _norm_hex(compose_hash, 32, "quote compose_hash")
        if observed_compose != self.compose_hash:
            raise MeasurementError(
                "compose_hash does not match pinned release "
                f"(expected {self.compose_hash}, got {observed_compose})"
            )
        observed = {reg: _norm_hex(measurements.get(reg), 48, f"quote {reg}") for reg in _BASE_REGISTERS}
        for base in self.base_images:
            if base.registers() == observed:
                return base
        diffs = [
            f"{reg}={observed[reg]}"
            for reg in _BASE_REGISTERS
            if all(base.registers()[reg] != observed[reg] for base in self.base_images)
        ]
        raise MeasurementError(
            "base-image measurements (MRTD/RTMR0..2) match no pinned dstack_base_images entry; "
            f"unmatched registers: {', '.join(diffs) or 'none individually, but no full match'}"
        )


def default_measurements_path() -> Path | None:
    """Path to the measurements.json shipped inside the client package, if present.

    The release process copies the published `measurements.json` into the package
    so `--measurement-policy pinned` works without an explicit `--measurements`.
    Returns None when no packaged manifest is bundled (e.g. an unreleased build).
    """
    try:
        candidate = resources.files("voxterm_sink_client") / MEASUREMENTS_FILENAME
        if candidate.is_file():
            return Path(str(candidate))
    except (FileNotFoundError, ModuleNotFoundError, TypeError):
        pass
    return None


def load_release(path: str | Path | None) -> ReleaseMeasurements:
    """Resolve and load a release manifest for pinned verification.

    Explicit `path` wins; otherwise fall back to the packaged manifest. Raises
    MeasurementError with actionable guidance if neither is available.
    """
    if path is not None:
        return ReleaseMeasurements.load(path)
    packaged = default_measurements_path()
    if packaged is not None:
        return ReleaseMeasurements.load(packaged)
    raise MeasurementError(
        "pinned verification requires a measurements manifest, but none is bundled "
        "with this client. Pass --measurements PATH pointing at the published "
        "measurements.json for this release."
    )
