from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .identity import config_dir


def trust_store_path() -> Path:
    return config_dir() / "verified_sinks.json"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TrustStore:
    def __init__(self, path: Path | None = None):
        self.path = path or trust_store_path()
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "url_index": {}, "sinks": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data.setdefault("schema_version", 1)
        data.setdefault("url_index", {})
        data.setdefault("sinks", {})
        return data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def apply(self, sink_url: str, verified: dict[str, Any]) -> None:
        pubkey = verified["sink_sig_pubkey"]
        known_for_url = self.data["url_index"].get(sink_url)
        if known_for_url and known_for_url != pubkey:
            raise ValueError("trusted URL presented a different sink signing key")

        record = self.data["sinks"].get(pubkey)
        measurements = verified.get("measurements", {})
        if record is None:
            timestamp = now_utc()
            record = {
                "sink_sig_pubkey": pubkey,
                "app_id": verified.get("app_id", ""),
                "compose_hash": verified.get("compose_hash", ""),
                "measurements": measurements,
                "first_seen": timestamp,
                "last_verified": timestamp,
                "urls": [sink_url],
                "verifier": verified.get(
                    "verifier", {"provider": "phala-cloud-api", "summary": {}}
                ),
            }
            self.data["sinks"][pubkey] = record
        else:
            if record.get("compose_hash", "") != verified.get("compose_hash", ""):
                raise ValueError("trusted sink presented a different compose_hash")
            for key in ("mrtd", "rtmr0", "rtmr1", "rtmr2", "rtmr3"):
                if record.get("measurements", {}).get(key) != measurements.get(key):
                    raise ValueError(f"trusted sink presented a different {key.upper()}")
            if sink_url not in record["urls"]:
                record["urls"].append(sink_url)
                record["urls"].sort()
            record["last_verified"] = now_utc()

        self.data["url_index"][sink_url] = pubkey
        self.save()

    def reset_url(self, sink_url: str) -> bool:
        pubkey = self.data["url_index"].pop(sink_url, None)
        if not pubkey:
            self.save()
            return False
        record = self.data["sinks"].get(pubkey)
        if record:
            record["urls"] = [url for url in record.get("urls", []) if url != sink_url]
            if not record["urls"]:
                self.data["sinks"].pop(pubkey, None)
        self.save()
        return True

    def inspect_public(self) -> dict[str, Any]:
        return self.data
