"""Samsung Tag2 via LAN token sidecar — HA custom integration.

Architecture (spec.md): HA never talks to Samsung. The sidecar on the
Docker VM holds the banked JWT from the phase0 probe; this integration
polls the sidecar every 60s and gates every entity on token freshness.
No secrets in HA config — the config entry holds only the sidecar URL.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .api import SidecarClient, SidecarConnectionError
from .const import CONF_SIDECAR_URL, DOMAIN
from .coordinator import SamsungTag2Coordinator  # noqa: F401 - public re-export

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.DEVICE_TRACKER, Platform.SENSOR,
             Platform.BINARY_SENSOR, Platform.BUTTON]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = SidecarClient(entry.data[CONF_SIDECAR_URL])
    coordinator = SamsungTag2Coordinator(hass, client)

    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        raise
    except Exception as err:  # noqa: BLE001 - first refresh surprise → retry later
        raise ConfigEntryNotReady(f"sidecar not ready: {err}") from err

    if not coordinator.last_update_success:
        raise ConfigEntryNotReady("sidecar first refresh failed")

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "client": client,
        # {device_id: friendly_name} — the per-tag Samsung app label
        # (item.label from /tag/{uuid}), resolved by the coordinator.
        "tags": {dev_id: state.get("friendly_name") or dev_id
                 for dev_id, state in (coordinator.data or {}).get("tags", {}).items()},
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.info("samsungtag2 configured: sidecar=%s tags=%d",
                 entry.data[CONF_SIDECAR_URL], len(hass.data[DOMAIN][entry.entry_id]["tags"]))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
