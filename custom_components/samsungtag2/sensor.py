"""Battery sensor per SmartTag2."""
from __future__ import annotations

from homeassistant.components.sensor import (SensorDeviceClass, SensorEntity,
                                             SensorStateClass)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    """Config-entry entrypoint — HA calls this directly per forwarded platform."""
    stored = hass.data[DOMAIN][entry.entry_id]
    coordinator = stored["coordinator"]
    async_add_entities([
        TagBatterySensor(coordinator, device_id, name or device_id)
        for device_id, name in stored["tags"].items()
    ])


class TagBatterySensor(CoordinatorEntity, SensorEntity):
    """Battery level for one SmartTag2, gated on token freshness (G3)."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator, device_id: str, name: str):
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{DOMAIN}_battery_{device_id}"
        self._attr_name = f"{name} Battery"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "name": name,
            "manufacturer": "Samsung",
            "model": "SmartTag2",
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
    def native_value(self):
        return self._tag().get("battery")
