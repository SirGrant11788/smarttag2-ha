# SamsungTag2 — Home Assistant custom integration

Reads Samsung Galaxy SmartTag2 data (location, battery, ring) via the sidecar token service on the LAN (smarttag-token-sidecar @ http://192.168.0.125:8788). Entities: per-tag device_tracker + battery sensor + ring button, plus a token_health binary sensor that flags when the banked token is stale (entities then go unavailable — never fake-fresh).

Install via HACS → Custom repositories → type "Integration" → install → restart HA → Settings → Add Integration → "samsungtag2" (default sidecar URL is pre-filled; press Configure).

## Per-tag names, entity ids and battery (v0.1.2)

Each tag is named from Samsung's own friendly label — the name you set in
the Samsung/Find app (e.g. **Max**, **Oreo**, **Tag Logan Car Key**) — read
from the per-tag detail endpoint on every 60s poll. The raw Samsung model
string (`EI-T5600` / `Tag2(UWB)`) is kept as the device *model* only, never
as the name. Name resolution order per tag:

1. `label` — the app's friendly name (what you set in the Find app)
2. Samsung's short device name, if no label is set
3. `SmartTag2 <last 6 of the tag id>` — only when both are missing, so two
   unnamed tags can never collide

Battery comes from the same per-tag payload and follows the app's own
semantics: FULL = 100 %, MEDIUM = 60 %, LOW = 25 % (the tag *list* endpoint
reports FULL for everything and is used only as a fallback). The tracker
also exposes `d2d_status` — which device the tag currently speaks BLE to.

New installs register human entity ids straight away:
`device_tracker.max`, `sensor.tag_logan_car_key_battery`,
`button.oreo_ring`. On an existing install the entity ids already in the
registry are left untouched (nothing is orphaned or renamed under you) —
rename them yourself if you want the new slugs.

### Renaming in the HA UI (safe — label mapping never breaks)

You can freely rename a device or entity in Home Assistant:

- **Device:** Settings → Devices & Services → select the tag → device
  name → pencil. HA stores this as *your* name and it always wins over the
  integration-supplied one.
- **Entity:** Settings → Devices & Services → Entities → filter by the
  tag → gear icon → rename (this sets a user-preferred name/entity id).

The integration never overwrites a user rename — it only (re)applies names
to fields you haven't overridden, at setup/reload time. If you change the
label inside the Samsung app, the next reload of the integration adopts the
new label (device names follow; your UI renames stay put).

## Changelog

- **v0.1.2** — devices named from the per-tag Samsung app label instead of
  the model string; label-slug entity ids for new installs; per-tag battery
  from the detail payload (app-accurate FULL/MEDIUM/LOW mapping); `d2d_status`
  tracker attribute; `EI-T5600` demoted to device model.
- v0.1.1 — sidecar upstream header fix (exact-case `x-sec-*` headers).
- v0.1.0 — initial release: per-tag tracker/battery/ring + token_health.

Phase 1 of the smarttag2 homelab project. Not affiliated with Samsung.
