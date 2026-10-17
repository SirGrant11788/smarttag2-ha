"""Config flow: one field — the sidecar base URL (default: LAN Docker VM).

No secrets in the entry. /jwt never flows through here; HA only ever
learns the sidecar's address (spec.md: "no secrets in HA config").
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries

from .api import SidecarClient, SidecarConnectionError
from .const import CONF_SIDECAR_URL, DEFAULT_SIDECAR_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class SamsungTag2ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Ask for the sidecar URL, probe /healthz, store the entry."""

    VERSION = 1

    @staticmethod
    def _schema():
        return vol.Schema({
            vol.Required(CONF_SIDECAR_URL, default=DEFAULT_SIDECAR_URL): str,
        })

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_SIDECAR_URL].strip().rstrip("/")
            try:
                client = SidecarClient(url, timeout=5)
                await self.hass.async_add_executor_job(client.healthz)
            # No SidecarStale branch: /healthz answers 200 with the staleness
            # in the body, so the probe can never raise it — the runtime
            # condition is surfaced by the token_health sensor, not here.
            except (SidecarConnectionError, Exception) as err:  # noqa: BLE001
                _LOGGER.warning("sidecar probe failed for %s: %r", url, err)
                errors["base"] = "cannot_connect"
            if not errors:
                return self.async_create_entry(
                    title="Samsung Tag2",
                    data={CONF_SIDECAR_URL: url},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self._schema(),
            errors=errors,
        )
