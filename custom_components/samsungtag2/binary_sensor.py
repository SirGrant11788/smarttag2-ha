"""token_health binary_sensor — first-class per spec.md observability.

on  = sidecar reachable AND bank fresh (safe to trust entity values)
off = sidecar reachable BUT bank stale (re-login prompt automation watches this)
unavailable = sidecar unreachable / coordinator failed entirely
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import (BinarySensorDeviceClass,
                                                    BinarySensorEntity)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    """Config-entry entrypoint — one token_health sensor per entry (not per tag)."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([TokenHealthSensor(coordinator)])


class TokenHealthSensor(CoordinatorEntity, BinarySensorEntity):
    """Is the banked Samsung token currently fresh?"""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = "Token Health"
    _attr_icon = "mdi:key-variant"

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_token_health"

    def _health(self) -> dict:
        return (self.coordinator.data or {}).get("token_health", {})

    @property
    def available(self) -> bool:
        # Unreachable sidecar → coordinator failure → unavailable.
        # Reachable-but-stale → available, is_on False (the G3 off signal).
        return (self.coordinator.last_update_success
                and bool(self._health().get("available")))

    @property
    def is_on(self):
        return bool(self._health().get("fresh"))

    @property
    def extra_state_attributes(self):
        health = self._health()
        return {
            "sha12": health.get("sha12"),
            "family": health.get("family"),
            "ttl_min": health.get("ttl_min"),
            "bank_ts": health.get("bank_ts"),
            "reason": health.get("reason"),
        }
