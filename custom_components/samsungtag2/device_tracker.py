"""device_tracker per SmartTag2 — location from sidecar /geolocation."""
from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.components.device_tracker.const import SourceType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    """Config-entry entrypoint — HA calls this directly per forwarded platform."""
    stored = hass.data[DOMAIN][entry.entry_id]
    coordinator = stored["coordinator"]
    async_add_entities([
        TagDeviceTracker(coordinator, device_id, name or device_id)
        for device_id, name in stored["tags"].items()
    ])


def _gps_date(raw_ms):
    if not raw_ms:
        return None
    try:
        return datetime.fromtimestamp(raw_ms / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


class TagDeviceTracker(CoordinatorEntity, TrackerEntity):
    """GPS tracker for one SmartTag2, gated on token freshness (G3)."""

    _attr_source_type = SourceType.GPS

    def __init__(self, coordinator, device_id: str, name: str):
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{DOMAIN}_tracker_{device_id}"
        # `name` is the per-tag friendly name resolved by the coordinator
        # (Samsung app label, e.g. 'Max' / 'Tag Logan Car Key'). Render it
        # directly so the map/list show the label, never 'Tag2(UWB)'.
        self._attr_name = name or device_id
        # First-registration entity_id slug: device_tracker.max,
        # device_tracker.tag_logan_car_key. Existing registry entries keep
        # their entity_id — suggested_object_id only applies at registration.
        self._attr_suggested_object_id = (slugify(name) if name else slugify(device_id))
        tag_state = ((coordinator.data or {}).get("tags", {})
                     .get(device_id, {}))
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "name": name or device_id,
            "manufacturer": "Samsung",
            "model": tag_state.get("device_model") or "SmartTag2",
        }

    def _tag(self) -> dict:
        return (self.coordinator.data or {}).get("tags", {}).get(self._device_id, {})

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        health = (self.coordinator.data or {}).get("token_health", {})
        if not health.get("fresh"):
            return False
        return bool(self._tag().get("update_success"))

    @property
    def latitude(self):
        return self._tag().get("latitude")

    @property
    def longitude(self):
        return self._tag().get("longitude")

    @property
    def location_accuracy(self):
        return self._tag().get("accuracy")

    @property
    def battery_level(self):
        return self._tag().get("battery")

    @property
    def extra_state_attributes(self):
        tag = self._tag()
        return {
            "device_id": self._device_id,
            "friendly_name": tag.get("friendly_name"),
            "samsung_name": tag.get("samsung_name"),
            "device_model": tag.get("device_model"),
            "d2d_status": tag.get("d2d_status"),
            "location_found": tag.get("location_found", False),
            "last_seen_ms": tag.get("gps_date"),
            "last_seen": _gps_date(tag.get("gps_date")),
        }
