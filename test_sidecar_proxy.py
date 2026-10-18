"""Sidecar proxy handlers — /tags, /tag/{uuid}, /geolocation/{uuid}, /ring/{uuid}.

Each proxies the matching api.samsungfind.com endpoint using the banked
token + api_headers.json companion pair, so HA never talks to Samsung.
Upstream is the local MockUpstream; zero live Samsung calls.
"""
import json
import urllib.error
import urllib.request

from conftest import make_token, sha12_of, write_bank, write_headers

API_PATHS = {
    "tags": "/devices",
    "tag": "/tag/devices",
    "geo": "/tag/geolocations",
}


def _call(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def _fresh_bank(ctx, clock, token=None):
    token = token or make_token(203)
    write_bank(ctx["data_dir"], token, exp=clock.now() + 3420, clock=clock)
    write_headers(ctx["data_dir"], userid="user-123", countrycode="ZAF")
    return token


def _auth_headers(upstream):
    last = upstream.requests[-1]["headers"]
    return {k.lower(): v for k, v in last.items()}


def test_upstream_headers_exact_case(start_sidecar, upstream, clock):
    """Samsung matches custom x-sec-* headers EXACTLY; urllib's add_header
    silently .capitalize()s the first letter ("x-sec-sa-userid" ->
    "X-sec-sa-userid") which upstream rejects with FIND400N002. The proxy
    must set headers directly on req.headers to preserve case (root cause
    of the 2026-10-18 live /tags 400; see qa-report.md addendum).
    """
    base, ctx = start_sidecar()
    _fresh_bank(ctx, clock)
    upstream.set("GET", "/devices", 200, {"items": []})

    status, _body = _call(base, "GET", "/tags")

    assert status == 200
    sent = _auth_headers(upstream)
    # exact-case custom headers present verbatim...
    assert "x-sec-sa-userid" in sent
    assert "x-sec-sa-countrycode" in sent
    assert "x-sec-sa-authtoken" in sent
    assert "x-sec-find-client-type" in sent
    # ...and NO mangled-capitalized versions
    assert "X-sec-sa-userid" not in sent
    assert "X-sec-sa-authtoken" not in sent


# ---------------------------------------------------------------------------
# /tags
# ---------------------------------------------------------------------------
def test_tags_proxies_devices_with_banked_auth(start_sidecar, upstream, clock):
    base, ctx = start_sidecar()
    token = _fresh_bank(ctx, clock)
    upstream.set("GET", "/devices", 200, {"items": [{"deviceId": "tag-0001"}]})

    status, body = _call(base, "GET", "/tags")

    assert status == 200
    assert body["items"] == [{"deviceId": "tag-0001"}]
    req = upstream.requests[-1]
    assert req["path"] == "/devices"
    assert "userId=user-123" in req["query"]
    assert "type=TAG" in req["query"]
    headers = _auth_headers(upstream)
    assert headers["x-sec-sa-authtoken"] == token
    assert headers["x-sec-sa-userid"] == "user-123"
    assert headers["x-sec-sa-countrycode"] == "ZAF"


def test_tags_stale_bank_503_without_upstream_call(start_sidecar, upstream, clock):
    base, ctx = start_sidecar()
    write_bank(ctx["data_dir"], make_token(), exp=clock.now() - 1, clock=clock)
    upstream.set("GET", "/devices", 200, {"items": []})

    status, body = _call(base, "GET", "/tags")

    assert status == 503
    assert body["state"] == "stale"
    assert upstream.requests == []


def test_tags_missing_headers_file_503(start_sidecar, upstream, clock):
    """api_headers.json is the companion contract; absent = cannot proxy."""
    base, ctx = start_sidecar(with_headers=False)
    write_bank(ctx["data_dir"], make_token(), exp=clock.now() + 600, clock=clock)
    upstream.set("GET", "/devices", 200, {"items": []})

    status, body = _call(base, "GET", "/tags")

    assert status == 503
    assert body["state"] == "no_headers"
    assert upstream.requests == []


# ---------------------------------------------------------------------------
# /tag/{uuid}
# ---------------------------------------------------------------------------
def test_tag_detail_proxies_tag_devices(start_sidecar, upstream, clock):
    base, ctx = start_sidecar()
    _fresh_bank(ctx, clock)
    upstream.set("GET", "/tag/devices/tag-0001", 200, {"item": {"label": "Max"}})

    status, body = _call(base, "GET", "/tag/tag-0001")

    assert status == 200
    assert body["item"]["label"] == "Max"
    assert upstream.requests[-1]["path"] == "/tag/devices/tag-0001"


def test_tag_rejects_malformed_uuid(start_sidecar, clock):
    base, ctx = start_sidecar()
    _fresh_bank(ctx, clock)

    status, body = _call(base, "GET", "/tag/..%2F..%2Fetc")

    assert status == 400
    assert body["error"] == "bad_uuid"


# ---------------------------------------------------------------------------
# /geolocation/{uuid}
# ---------------------------------------------------------------------------
def test_geolocation_proxies_with_device_id_param(start_sidecar, upstream, clock):
    base, ctx = start_sidecar()
    _fresh_bank(ctx, clock)
    upstream.set("GET", "/tag/geolocations", 200,
                 {"items": [{"geolocations": [{"latitude": -26.2, "longitude": 28.0,
                                               "valid": True, "lastUpdateTime": 5}]}]})

    status, body = _call(base, "GET", "/geolocation/tag-0001")

    assert status == 200
    req = upstream.requests[-1]
    assert req["path"] == "/tag/geolocations"
    assert "deviceId=tag-0001" in req["query"]
    assert "userId=user-123" in req["query"]
    assert body["items"][0]["geolocations"][0]["latitude"] == -26.2


def test_geolocation_caches_upstream_per_device(start_sidecar, upstream, clock):
    """Passive-poll budget: repeated reads inside the cache window hit the bank, not Samsung."""
    base, ctx = start_sidecar(cache_ttl_geo_s=900)
    _fresh_bank(ctx, clock)
    upstream.set("GET", "/tag/geolocations", 200, {"items": [{"geolocations": []}]})

    for _ in range(3):
        status, _ = _call(base, "GET", "/geolocation/tag-0001")
        assert status == 200
    assert len(upstream.requests) == 1

    clock.advance(901)
    status, _ = _call(base, "GET", "/geolocation/tag-0001")
    assert status == 200
    assert len(upstream.requests) == 2


# ---------------------------------------------------------------------------
# /ring/{uuid}
# ---------------------------------------------------------------------------
def test_ring_proxies_operation_ring_payload(start_sidecar, upstream, clock):
    base, ctx = start_sidecar()
    _fresh_bank(ctx, clock)
    upstream.set("POST", "/operation", 200, {"result": "ok"})

    status, body = _call(base, "POST", "/ring/tag-0001")

    assert status == 200
    assert body == {"result": "ok"}
    req = upstream.requests[-1]
    assert req["path"] == "/operation"
    assert json.loads(req["body"]) == {"deviceId": "tag-0001", "type": "RING",
                                       "userId": "user-123"}
    headers = _auth_headers(upstream)
    assert headers["x-sec-sa-authtoken"]


def test_ring_stale_bank_503(start_sidecar, upstream, clock):
    base, ctx = start_sidecar()
    write_bank(ctx["data_dir"], make_token(), exp=clock.now() - 60, clock=clock)
    write_headers(ctx["data_dir"])

    status, body = _call(base, "POST", "/ring/tag-0001")

    assert status == 503
    assert body["state"] == "stale"
    assert upstream.requests == []


def test_ring_upstream_auth_failure_maps_502(start_sidecar, upstream, clock):
    base, ctx = start_sidecar()
    _fresh_bank(ctx, clock)
    upstream.set("POST", "/operation", 401, {"error": "unauthorized"})

    status, body = _call(base, "POST", "/ring/tag-0001")

    assert status == 502
    assert body["state"] == "upstream_auth"


def test_ring_rejects_oversized_declared_body(start_sidecar, upstream, clock):
    """N3: the /ring body is discarded — a >1KB Content-Length gets 413,
    never an unbounded read (or an upstream ring)."""
    import http.client

    base, ctx = start_sidecar()
    _fresh_bank(ctx, clock)
    upstream.set("POST", "/operation", 200, {"result": "ok"})
    host, port = base.removeprefix("http://").split(":")

    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    conn.putrequest("POST", "/ring/tag-0001")
    conn.putheader("Content-Length", "100000")
    conn.endheaders()
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()

    assert resp.status == 413
    assert body["error"] == "body_too_large"
    assert upstream.requests == []


# ---------------------------------------------------------------------------
# Negative caching (QA B2): upstream ANSWERS are cached, whatever the status
# ---------------------------------------------------------------------------
def test_tags_upstream_401_is_cached_not_retried(start_sidecar, upstream, clock):
    """A revoked/re-minted token (401) must consume the cache window, not
    bypass it: repeated HA polls inside the window hit the cache, never
    Samsung — and the cached 401 body is replayed unchanged."""
    base, ctx = start_sidecar(cache_ttl_tags_s=1800)
    _fresh_bank(ctx, clock)
    upstream.set("GET", "/devices", 401, {"error": "revoked"})

    answers = []
    for _ in range(5):
        status, body = _call(base, "GET", "/tags")
        answers.append((status, body.get("state"), body.get("upstream_status")))

    assert answers == [(502, "upstream_auth", 401)] * 5
    assert len(upstream.requests) == 1     # 5 polls → 1 Samsung call

    clock.advance(1801)                    # window expires → one fresh attempt
    status, body = _call(base, "GET", "/tags")
    assert status == 502
    assert body["state"] == "upstream_auth"
    assert len(upstream.requests) == 2


def test_tags_upstream_500_is_cached_not_retried(start_sidecar, upstream, clock):
    """Same bound for any 5xx: the failure body replays from cache."""
    base, ctx = start_sidecar(cache_ttl_tags_s=1800)
    _fresh_bank(ctx, clock)
    upstream.set("GET", "/devices", 500, {"error": "boom"})

    for _ in range(4):
        status, body = _call(base, "GET", "/tags")
        assert status == 500
        assert body == {"error": "boom"}
    assert len(upstream.requests) == 1


def test_tags_stale_bank_503_is_not_cached(start_sidecar, upstream, clock):
    """Bank-gate refusals never reach upstream, so they must NOT be cached —
    a stale-bank 503 has to clear the moment the bank refreshes."""
    base, ctx = start_sidecar(cache_ttl_tags_s=1800)
    write_bank(ctx["data_dir"], make_token(), exp=clock.now() - 1, clock=clock)
    write_headers(ctx["data_dir"])
    upstream.set("GET", "/devices", 200, {"items": [{"deviceId": "tag-0001"}]})

    status, body = _call(base, "GET", "/tags")
    assert status == 503
    assert body["state"] == "stale"

    _fresh_bank(ctx, clock)                # phase0 re-mints → immediate recovery
    status, body = _call(base, "GET", "/tags")
    assert status == 200
    assert len(upstream.requests) == 1


# ---------------------------------------------------------------------------
# Logging hygiene
# ---------------------------------------------------------------------------
def test_sidecar_never_logs_raw_tokens(start_sidecar, clock, caplog):
    import logging
    base, ctx = start_sidecar()
    token = _fresh_bank(ctx, clock)

    with caplog.at_level(logging.DEBUG):
        status, _ = _call(base, "GET", "/jwt")
    assert status == 200

    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert token not in joined
    assert sha12_of(token) in joined
