"""Environment-driven configuration for the sink (spec §7.1, §8.3)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger("voxterm.sink")

DEFAULT_READ_SECRET = "1234"

# Spec §7.1: default RECOMMENDED caps.
DEFAULT_MAX_CHUNK_BYTES = 64 * 1024
DEFAULT_MAX_TRANSCRIPT_BYTES = 16 * 1024 * 1024
DEFAULT_RATE_PER_MIN = 600


@dataclass
class Settings:
    # Read tier (§8.3): single shared static secret, operator-configurable.
    read_secret: str = DEFAULT_READ_SECRET
    # Optional coordinator secret (§8.3): defined, not enforced beyond a stub.
    coordinator_secret: str | None = None

    # Attestation backend: "dstack" (default, real TD) or "dev" (insecure stub).
    attest_mode: str = "dstack"
    # Optional dstack guest-agent endpoint. Empty → the SDK default unix socket
    # (/var/run/dstack.sock). Set to a URL (e.g. http://localhost:8090) to drive
    # the dstack simulator for local development of the real code path.
    dstack_endpoint: str | None = None
    # Deterministic seed for the dev identity so keys survive restarts. Used
    # ONLY in dev mode — dstack mode fails closed instead of seeding (§5.2).
    dev_seed: str = "voxterm-sink-dev-seed"

    # Limits (§7.1). Size caps (max_chunk/transcript_bytes) ARE enforced in-app
    # (413). rate_per_min is ADVISORY: per spec §11.8 the primary DoS layer is
    # dstack-gateway (which sees real client IPs; the app sees only the gateway
    # behind TLS termination, §11.2), so the sink advertises the limit but does
    # not enforce it in-process. See docs/DEVELOPMENT.md "Rate limiting".
    max_chunk_bytes: int = DEFAULT_MAX_CHUNK_BYTES
    max_transcript_bytes: int = DEFAULT_MAX_TRANSCRIPT_BYTES
    rate_per_min: int = DEFAULT_RATE_PER_MIN

    # Optional JSON snapshot path for crude persistence across restarts.
    snapshot_path: str | None = None

    # Token lifetime in seconds for POST /v1/auth.
    token_ttl_seconds: int = 3600

    hiveminds: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        s = cls(
            read_secret=e.get("VOXTERM_SINK_READ_SECRET", DEFAULT_READ_SECRET),
            coordinator_secret=e.get("VOXTERM_SINK_COORDINATOR_SECRET") or None,
            attest_mode=e.get("VOXTERM_SINK_ATTEST", "dstack").lower(),
            dstack_endpoint=(
                e.get("VOXTERM_SINK_DSTACK_ENDPOINT")
                or e.get("DSTACK_SIMULATOR_ENDPOINT")
                or None
            ),
            dev_seed=e.get("VOXTERM_SINK_DEV_SEED", "voxterm-sink-dev-seed"),
            snapshot_path=e.get("VOXTERM_SINK_SNAPSHOT") or None,
            token_ttl_seconds=int(e.get("VOXTERM_SINK_TOKEN_TTL", "3600")),
        )
        if "VOXTERM_SINK_MAX_CHUNK_BYTES" in e:
            s.max_chunk_bytes = int(e["VOXTERM_SINK_MAX_CHUNK_BYTES"])
        if "VOXTERM_SINK_MAX_TRANSCRIPT_BYTES" in e:
            s.max_transcript_bytes = int(e["VOXTERM_SINK_MAX_TRANSCRIPT_BYTES"])
        return s

    def warn_insecure(self) -> None:
        # Spec §8.3: MUST log a startup warning if the read secret is the default.
        if self.read_secret == DEFAULT_READ_SECRET:
            log.warning(
                "VOXTERM_SINK_READ_SECRET is the default placeholder %r — this is "
                "NOT real authentication (spec §8.3). Set it before any real use.",
                DEFAULT_READ_SECRET,
            )
        if self.attest_mode == "dev":
            log.warning(
                "VOXTERM_SINK_ATTEST=dev: attestation quotes are FABRICATED and "
                "carry NO hardware guarantee. Development only (spec §5, §6)."
            )
