#!/usr/bin/env python3
"""SmartTag2 token sidecar — serves the phase0 token bank to HA on the LAN.

Contract (sdlc/smarttag2-ha/spec.md + phase1 brief):
- GET /jwt              → 200 {token, sha12, exp, ttl_min, family, bank_ts}
                          or 503 {state: stale, reason} when the bank is
                          empty / expired / ttl < min_ttl_s / wrong family.
- GET /healthz          → {state, sha12, ttl_min, family, bank_ts, ...}
- GET /tags             → proxies GET  api.samsungfind.com/devices?type=TAG
- GET /tag/{uuid}       → proxies GET  api.samsungfind.com/tag/devices/{uuid}
- GET /geolocation/{id} → proxies GET  api.samsungfind.com/tag/geolocations
- POST /ring/{uuid}     → proxies POST api.samsungfind.com/operation (RING)

The sidecar reads the SAME /data volume as phase0's probe — it never
touches the browser, never mints tokens, and never talks to HA's
Home Assistant API. HA never talks to Samsung: every Samsung call goes
through here with the banked token + the api_headers.json companion pair.

Security rules baked in:
- raw tokens never appear in log lines (sha12 fingerprint only);
- upstream token only leaves this process inside the Samsung request;
- /jwt 503 bodies carry state/reason, never a token.

Stdlib only — no pip deps, so the container is a plain python:alpine.
"""
from __future__ import annotations

import json
import http.client
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_LOGGER = logging.getLogger("smarttag.sidecar")

DEFAULT_UPSTREAM = "https://api.samsungfind.com"
UUID_RE = re.compile(r"^[A-Za-z0-9-]{6,64}$")
GOOGLE_TOKEN_MIN_LEN = 400  # family gate: Google-OIDC JWTs are >400 chars
RING_BODY_MAX_BYTES = 1024  # /ring body is discarded — cap the read hard


@dataclass
class SidecarConfig:
    data_dir: str = os.environ.get("DATA_DIR", "/data")
    upstream_base: str = os.environ.get("SAMSUNG_API_BASE", DEFAULT_UPSTREAM)
    min_ttl_s: int = int(os.environ.get("SIDECAR_MIN_TTL_S", "60"))
    cache_ttl_tags_s: int = int(os.environ.get("SIDECAR_CACHE_TAGS_S", "1800"))
    cache_ttl_geo_s: int = int(os.environ.get("SIDECAR_CACHE_GEO_S", "900"))
    upstream_timeout_s: float = float(os.environ.get("SIDECAR_UPSTREAM_TIMEOUT_S", "10"))
    clock: Callable[[], float] = field(default=time.time)


# ---------------------------------------------------------------------------
# Token-bank reading + family gate
# ---------------------------------------------------------------------------
@dataclass
class BankSnapshot:
    fresh: bool
    reason: str = ""
    token: str = ""
    sha12: str = ""
    exp: float | None = None
    ttl_min: float | None = None
    family: str = ""
    bank_ts: str | None = None


def read_bank(cfg: SidecarConfig) -> BankSnapshot:
    """Apply the family gate to /data/latest_token.json.

    Fresh only when: file exists, family == samsung, token length < 400,
    exp is in the future, and remaining ttl >= min_ttl_s.
    """
    path = os.path.join(cfg.data_dir, "latest_token.json")
    try:
        with open(path) as fh:
            record = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return BankSnapshot(fresh=False, reason="empty")

    if not isinstance(record, dict):
        # Valid JSON but the wrong shape (e.g. a bare list) — an unreadable
        # bank must answer like an empty one, not 500 on record.get().
        return BankSnapshot(fresh=False, reason="empty")

    token = record.get("token") or ""
    family = record.get("family") or ""
    exp = record.get("exp")

    if not token:
        return BankSnapshot(fresh=False, reason="empty")
    if family != "samsung" or len(token) >= GOOGLE_TOKEN_MIN_LEN:
        _LOGGER.warning("bank gate: non-samsung family=%s len=%d sha12=%s",
                        family, len(token), _sha12(token))
        return BankSnapshot(fresh=False, reason="family")
    if not isinstance(exp, (int, float)):
        return BankSnapshot(fresh=False, reason="expired")

    ttl_s = exp - cfg.clock()
    if ttl_s <= 0:
        return BankSnapshot(fresh=False, reason="expired")
    if ttl_s < cfg.min_ttl_s:
        return BankSnapshot(fresh=False, reason="ttl_low")

    return BankSnapshot(
        fresh=True,
        token=token,
        sha12=_sha12(token),
        exp=exp,
        ttl_min=round(ttl_s / 60, 1),
        family=family,
        bank_ts=record.get("ts_iso"),
    )


def read_companion_headers(cfg: SidecarConfig) -> dict | None:
    path = os.path.join(cfg.data_dir, "api_headers.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not data.get("x-sec-sa-userid") or not data.get("x-sec-sa-countrycode"):
        return None
    return data


def _sha12(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Upstream proxy with a small response cache
# ---------------------------------------------------------------------------
class UpstreamProxy:
    """GET-through cache + token-stamped forwarding to api.samsungfind.com.

    The cache exists for the passive-poll budget in the spec (≤4 calls/hour,
    15–30 min/tag): HA polls the sidecar freely; the sidecar polls Samsung
    no faster than the per-endpoint TTLs. Writes (ring) are never cached.
    """

    def __init__(self, cfg: SidecarConfig):
        self._cfg = cfg
        self._cache: dict[str, tuple[float, int, dict]] = {}
        self._lock = threading.Lock()

    def _cached_get(self, url: str, ttl_s: int):
        now = self._cfg.clock()
        with self._lock:
            hit = self._cache.get(url)
            if hit and now - hit[0] < ttl_s:
                return hit[1], hit[2]
        status, body, upstream_answered = self._request("GET", url)
        if upstream_answered:
            # Negative caching: EVERY upstream answer is cached — success or
            # failure (401/403 re-mint, 5xx, rate-limit) — so a failing
            # upstream consumes the tunable window instead of bypassing it
            # (per-poll retries against Samsung are the lockout vector the
            # spec bans). Not cached: bank-gate refusals (a stale-bank 503
            # must clear the moment the bank refreshes) and transport errors
            # (a network blip must not freeze data for a whole TTL).
            with self._lock:
                self._cache[url] = (now, status, body)
        return status, body

    def get_tags(self, snapshot: BankSnapshot, companion: dict) -> tuple[int, dict]:
        url = (f"{self._cfg.upstream_base}/devices?"
               + urllib.parse.urlencode({
                   "userId": companion["x-sec-sa-userid"],
                   "type": "TAG",
               }))
        return self._cached_get(url, self._cfg.cache_ttl_tags_s)

    def get_tag(self, snapshot: BankSnapshot, companion: dict, uuid: str) -> tuple[int, dict]:
        url = f"{self._cfg.upstream_base}/tag/devices/{uuid}"
        return self._cached_get(url, self._cfg.cache_ttl_tags_s)

    def get_geolocation(self, snapshot: BankSnapshot, companion: dict, uuid: str) -> tuple[int, dict]:
        url = (f"{self._cfg.upstream_base}/tag/geolocations?"
               + urllib.parse.urlencode({
                   "deviceId": uuid,
                   "userId": companion["x-sec-sa-userid"],
               }))
        return self._cached_get(url, self._cfg.cache_ttl_geo_s)

    def ring(self, snapshot: BankSnapshot, companion: dict, uuid: str) -> tuple[int, dict]:
        url = f"{self._cfg.upstream_base}/operation"
        payload = json.dumps({
            "deviceId": uuid,
            "type": "RING",
            "userId": companion["x-sec-sa-userid"],
        }).encode()
        return self._request("POST", url, data=payload)[:2]

    def _request(self, method: str, url: str,
                 data: bytes | None = None) -> tuple[int, dict, bool]:
        """Forward one request to Samsung.

        Returns (status, body, upstream_answered). upstream_answered is True
        only when Samsung actually answered with an HTTP status (any code) —
        that is the only case the response cache may store.
        """
        snapshot = read_bank(self._cfg)  # re-read: never forward a stale token
        if not snapshot.fresh:
            return 503, {"state": "stale", "reason": snapshot.reason}, False
        companion = read_companion_headers(self._cfg)
        if companion is None:
            return 503, {"state": "no_headers"}, False

        req_url = urllib.parse.urlparse(url)
        conn_cls = (http.client.HTTPSConnection if req_url.scheme == "https"
                    else http.client.HTTPConnection)
        conn = conn_cls(req_url.netloc, timeout=self._cfg.upstream_timeout_s)
        headers = {
            "x-sec-sa-authtoken": snapshot.token,
            "x-sec-sa-userid": companion["x-sec-sa-userid"],
            "x-sec-sa-countrycode": companion["x-sec-sa-countrycode"],
            "x-sec-find-client-type": "web",
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        path = req_url.path + ("?" + req_url.query if req_url.query else "")

        try:
            # http.client writes header names VERBATIM — required because
            # Samsung matches custom x-sec-* headers case-sensitively.
            # urllib.request is NOT usable here even with direct
            # req.headers assignment: AbstractHTTPHandler.do_open
            # title-cases every outgoing header name, and Samsung rejects
            # X-Sec-Sa-Userid with FIND400N002 "Required header not
            # found" (verified on the wire 2026-10-18).
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            status, raw = resp.status, resp.read()
        except http.client.HTTPException as exc:
            conn.close()
            _LOGGER.error("upstream http error: %r (sha12=%s)",
                          type(exc).__name__, snapshot.sha12)
            return 502, {"state": "upstream_unreachable"}, False
        except (TimeoutError, OSError) as exc:
            conn.close()
            _LOGGER.error("upstream unreachable: %r (sha12=%s)",
                          type(exc).__name__, snapshot.sha12)
            return 502, {"state": "upstream_unreachable"}, False
        conn.close()

        if status in (401, 403):
            _LOGGER.warning("upstream %s for %s %s (sha12=%s)",
                            status, method, req_url.path, snapshot.sha12)
            return 502, {"state": "upstream_auth", "upstream_status": status}, True

        _LOGGER.info("%s %s -> %s (sha12=%s)", method,
                     urllib.parse.urlparse(url).path, status, snapshot.sha12)
        try:
            return status, json.loads(raw), True
        except json.JSONDecodeError:
            return status, {"raw_len": len(raw)}, True


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class SidecarHandler(BaseHTTPRequestHandler):
    cfg: SidecarConfig
    proxy: UpstreamProxy
    server_version = "smarttag-sidecar/1.0"

    def do_GET(self):  # noqa: N802
        parts = urllib.parse.urlparse(self.path)
        path = parts.path.rstrip("/") or "/"

        if path == "/jwt":
            self._handle_jwt()
        elif path == "/healthz":
            self._handle_healthz()
        elif path == "/tags":
            self._proxy_get("get_tags")
        elif path.startswith("/geolocation/"):
            uuid = path[len("/geolocation/"):]
            if not UUID_RE.match(uuid):
                self._send(400, {"error": "bad_uuid"})
            else:
                self._proxy_get("get_geolocation", uuid)
        elif path.startswith("/tag/"):
            uuid = path[len("/tag/"):]
            if not UUID_RE.match(uuid):
                self._send(400, {"error": "bad_uuid"})
            else:
                self._proxy_get("get_tag", uuid)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        parts = urllib.parse.urlparse(self.path)
        path = parts.path.rstrip("/")
        if path.startswith("/ring/"):
            uuid = path[len("/ring/"):]
            if not UUID_RE.match(uuid):
                self._send(400, {"error": "bad_uuid"})
                return
            snapshot = read_bank(self.cfg)
            if not snapshot.fresh:
                self._send(503, {"state": "stale", "reason": snapshot.reason})
                return
            companion = read_companion_headers(self.cfg)
            if companion is None:
                self._send(503, {"state": "no_headers"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._send(400, {"error": "bad_content_length"})
                return
            if length > RING_BODY_MAX_BYTES:
                # The body is discarded anyway — never buffer a huge one.
                self._send(413, {"error": "body_too_large"})
                return
            if length > 0:
                self.rfile.read(length)  # ring payload is derived, not accepted
            status, body = self.proxy.ring(snapshot, companion, uuid)
            self._send(status, body)
        else:
            self._send(404, {"error": "not found"})

    # -- handlers ---------------------------------------------------------
    def _handle_jwt(self):
        snapshot = read_bank(self.cfg)
        if not snapshot.fresh:
            _LOGGER.info("jwt request: stale (reason=%s)", snapshot.reason)
            self._send(503, {"state": "stale", "reason": snapshot.reason})
            return
        _LOGGER.info("jwt request: fresh (sha12=%s ttl_min=%s)",
                     snapshot.sha12, snapshot.ttl_min)
        self._send(200, {
            "token": snapshot.token,
            "sha12": snapshot.sha12,
            "exp": int(snapshot.exp or 0),
            "ttl_min": snapshot.ttl_min,
            "family": snapshot.family,
            "bank_ts": snapshot.bank_ts,
        })

    def _handle_healthz(self):
        snapshot = read_bank(self.cfg)
        companion = read_companion_headers(self.cfg)
        self._send(200, {
            "state": "ok" if snapshot.fresh else "stale",
            "reason": snapshot.reason or None,
            "sha12": snapshot.sha12 or None,
            "ttl_min": snapshot.ttl_min,
            "family": snapshot.family or None,
            "bank_ts": snapshot.bank_ts,
            "companion_headers": bool(companion),
            "upstream_base": self.cfg.upstream_base,
            "uptime_s": round(self.cfg.clock() - _START_CLOCK, 1),
        })

    def _proxy_get(self, method_name: str, uuid: str | None = None):
        snapshot = read_bank(self.cfg)
        if not snapshot.fresh:
            self._send(503, {"state": "stale", "reason": snapshot.reason})
            return
        companion = read_companion_headers(self.cfg)
        if companion is None:
            self._send(503, {"state": "no_headers"})
            return
        method = getattr(self.proxy, method_name)
        if uuid is None:
            status, body = method(snapshot, companion)
        else:
            status, body = method(snapshot, companion, uuid)
        self._send(status, body)

    # -- plumbing ---------------------------------------------------------
    def _send(self, status: int, body: dict) -> None:
        blob = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        _LOGGER.debug("%s - %s", self.address_string(), format % args)


_START_CLOCK = time.time()


def create_server(cfg: SidecarConfig, host: str = "0.0.0.0", port: int = 8788) -> ThreadingHTTPServer:
    """Build a bound ThreadingHTTPServer (main() wires it to 0.0.0.0:8788)."""
    proxy = UpstreamProxy(cfg)

    class _Handler(SidecarHandler):
        pass

    _Handler.cfg = cfg
    _Handler.proxy = proxy
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("SIDECAR_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    host = os.environ.get("SIDECAR_BIND_HOST", "0.0.0.0")
    port = int(os.environ.get("SIDECAR_PORT", "8788"))
    cfg = SidecarConfig()
    server = create_server(cfg, host=host, port=port)
    _LOGGER.info("sidecar listening on %s:%d (upstream=%s data_dir=%s)",
                 host, port, cfg.upstream_base, cfg.data_dir)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
