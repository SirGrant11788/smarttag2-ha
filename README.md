# SamsungTag2 — Home Assistant custom integration

Reads Samsung Galaxy SmartTag2 data (location, battery, ring) via the sidecar token service on the LAN (smarttag-token-sidecar @ http://192.168.0.125:8788). Entities: per-tag device_tracker + battery sensor + ring button, plus a token_health binary sensor that flags when the banked token is stale (entities then go unavailable — never fake-fresh).

Install via HACS → Custom repositories → type "Integration" → install → restart HA → Settings → Add Integration → "samsungtag2" (default sidecar URL is pre-filled; press Configure).

Phase 1 of the smarttag2 homelab project. Not affiliated with Samsung.
