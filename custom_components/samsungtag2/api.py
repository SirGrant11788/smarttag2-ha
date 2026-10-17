"""Sidecar HTTP client — stdlib only, no aiohttp, no Samsung endpoints.

HA talks to THIS sidecar and nothing else. The sidecar holds the banked
token; this client never sees a raw token except in the /jwt response,
which it passes straight to callers without logging.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

_LOGGER = logging.getLogger(__name__)


class SidecarError(Exception):
    """Base for sidecar transport/protocol failures."""


class SidecarStale(SidecarError):
    """Sidecar answered 503 {state: stale} — token bank empty/expired."""

    def __init__(self, reason: str = ""):
        super().__init__(f"sidecar token bank stale ({reason or 'unknown'})")
        self.reason = reason


class SidecarConnectionError(SidecarError):
    """Sidecar unreachable (network/timeout)."""


class SidecarClient:
    """Blocking stdlib client; callers run it via hass.async_add_executor_job."""

    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- transport --------------------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None):
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                try:
                    payload = json.loads(resp.read())
                except json.JSONDecodeError as err:
                    # A 200 with a non-JSON body is a protocol failure — keep
                    # it inside the SidecarError taxonomy, never a raw
                    # JSONDecodeError escaping into the coordinator.
                    raise SidecarError(
                        f"sidecar {method} {path} -> non-JSON body") from err
                return resp.status, payload
        except urllib.error.HTTPError as err:
            try:
                payload = json.loads(err.read())
            except json.JSONDecodeError:
                payload = {}
            if err.code == 503 and payload.get("state") == "stale":
                raise SidecarStale(payload.get("reason", "")) from err
            raise SidecarError(f"sidecar {method} {path} -> HTTP {err.code}") from err
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise SidecarConnectionError(
                f"sidecar unreachable: {type(err).__name__}") from err

    # -- endpoints --------------------------------------------------------
    def get_jwt(self) -> dict:
        """200 contract: {token, sha12, exp, ttl_min, family, bank_ts}."""
        status, payload = self._request("GET", "/jwt")
        if status != 200:
            raise SidecarError(f"/jwt -> HTTP {status}")
        return payload

    def healthz(self) -> dict:
        status, payload = self._request("GET", "/healthz")
        if status != 200:
            raise SidecarError(f"/healthz -> HTTP {status}")
        return payload

    def get_tags(self) -> list[dict]:
        status, payload = self._request("GET", "/tags")
        if status != 200:
            raise SidecarError(f"/tags -> HTTP {status}")
        return payload.get("items", [])

    def get_tag(self, device_id: str) -> dict:
        status, payload = self._request("GET", f"/tag/{device_id}")
        if status != 200:
            raise SidecarError(f"/tag -> HTTP {status}")
        return payload.get("item", payload)

    def get_geolocation(self, device_id: str) -> dict:
        status, payload = self._request("GET", f"/geolocation/{device_id}")
        if status != 200:
            raise SidecarError(f"/geolocation -> HTTP {status}")
        return payload

    def ring(self, device_id: str) -> dict:
        status, payload = self._request("POST", f"/ring/{device_id}")
        if status != 200:
            raise SidecarError(f"/ring -> HTTP {status}")
        return payload
