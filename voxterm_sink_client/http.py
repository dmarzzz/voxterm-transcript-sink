from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class HTTPResult:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class HTTPTransport:
    def get(self, url: str, headers: dict[str, str] | None = None) -> HTTPResult:
        return self._request("GET", url, headers=headers)

    def post_json(
        self, url: str, payload: Any, headers: dict[str, str] | None = None
    ) -> HTTPResult:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        merged = {"Content-Type": "application/json"}
        merged.update(headers or {})
        return self._request("POST", url, body=body, headers=merged)

    def _request(
        self, method: str, url: str, body: bytes | None = None, headers: dict[str, str] | None = None
    ) -> HTTPResult:
        req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return HTTPResult(
                    resp.status,
                    {key.lower(): value for key, value in resp.headers.items()},
                    resp.read(),
                )
        except urllib.error.HTTPError as exc:
            return HTTPResult(
                exc.code,
                {key.lower(): value for key, value in exc.headers.items()},
                exc.read(),
            )
