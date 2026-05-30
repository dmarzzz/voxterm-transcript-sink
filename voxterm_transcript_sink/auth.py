"""v1 auth: the ``1234`` read-secret placeholder (spec §8).

Not real authentication — it exists so the read surface has a seam to upgrade
(§8.3, §12). Tokens are opaque and held in memory.
"""

from __future__ import annotations

import secrets
import threading
from datetime import datetime, timedelta, timezone

from .config import Settings


class TokenStore:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._lock = threading.RLock()
        self._tokens: dict[str, dict] = {}  # token -> {tier, expires_at(dt)}

    def issue(self, tier: str, secret: str) -> dict | None:
        """Return a token record for a valid (tier, secret), else None."""
        if tier == "cohort":
            ok = secret == self._settings.read_secret
        elif tier == "coordinator":
            ok = (
                self._settings.coordinator_secret is not None
                and secret == self._settings.coordinator_secret
            )
        else:
            ok = False
        if not ok:
            return None

        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(
            seconds=self._settings.token_ttl_seconds
        )
        with self._lock:
            self._tokens[token] = {"tier": tier, "expires_at": expires}
        return {
            "token": token,
            "tier": tier,
            "expires_at": expires.isoformat().replace("+00:00", "Z"),
        }

    def tier_of(self, token: str | None) -> str:
        """Resolve a bearer token to a tier, or 'public' if invalid/expired."""
        if not token:
            return "public"
        with self._lock:
            rec = self._tokens.get(token)
            if not rec:
                return "public"
            if rec["expires_at"] < datetime.now(timezone.utc):
                self._tokens.pop(token, None)
                return "public"
            return rec["tier"]

    def can_read(self, token: str | None) -> bool:
        # cohort ⊇ public; coordinator ⊇ cohort (spec §8.1).
        return self.tier_of(token) in ("cohort", "coordinator")
