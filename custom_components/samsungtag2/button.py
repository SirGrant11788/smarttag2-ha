"""Ring button per SmartTag2 — start-only (API cannot stop; spec.md)."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import SidecarError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Config-entry entrypoint — HA calls this directly per forwarded platform."""
    stored = hass.data[DOMAIN][entry.entry_id]
    coordinator = stored["coordinator"]
    async_add_entities([
        RingButton(coordinator, device_id, name or device_id)
        for device_id, name in stored["tags"].items()
    ])


class RingButton(CoordinatorEntity, ButtonEntity):
    """Make one SmartTag2 ring. Refuses to fire on a stale/unreachable token."""

    _attr_icon = "mdi:nfc-search-variant"

    def __init__(self, coordinator, device_id: str, name: str):
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{DOMAIN}_ring_{device_id}"
        self._attr_name = f"{name} Ring"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "name": name,
            "manufacturer": "Samsung",
            "model": "SmartTag2",
        }

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        health = (self.coordinator.data or {}).get("token_health", {})
        return bool(health.get("fresh"))

    async def async_press(self):
        if not self.available:
            _LOGGER.warning("ring ignored for %s: token not fresh", self._device_id)
            return
        try:
            await self.hass.async_add_executor_job(
                self.coordinator.client.ring, self._device_id)
            _LOGGER.info("ring sent for %s", self._attr_name)
        except SidecarError as err:
            _LOGGER.error("ring failed for %s: %r", self._attr_name, err)
