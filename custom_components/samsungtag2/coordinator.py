"""DataUpdateCoordinator: polls sidecar /jwt + /tags (+ geolocations).

Staleness semantics (spec.md G3):
- Sidecar reachable but bank stale (/jwt 503) → coordinator SUCCEEDS with
  token_health {fresh: False}; every per-tag entity checks token freshness
  in `available` and goes unavailable — never a stale-but-live value.
- Sidecar unreachable → UpdateFailed → last_update_success False →
  every CoordinatorEntity (incl. token_health) unavailable.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SidecarClient, SidecarConnectionError, SidecarError, SidecarStale
from .const import BATTERY_LEVELS, DOMAIN, UPDATE_INTERVAL_S

_LOGGER = logging.getLogger(__name__)


def parse_battery(raw) -> int | None:
    """Samsung battery: int/percent or one of FULL/MEDIUM/LOW/VERY_LOW."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        mapped = BATTERY_LEVELS.get(raw.upper())
        if mapped is not None:
            return mapped
        try:
            return int(raw)
        except ValueError:
            return None
    return None


def pick_geolocation(payload: dict) -> dict:
    """Most recent valid geolocation from a /tag/geolocations response."""
    items = payload.get("items") or []
    if not items:
        return {}
    geos = items[0].get("geolocations") or []
    best, best_time = None, -1
    for geo in geos:
        if not geo.get("valid", False):
            continue
        update_time = geo.get("lastUpdateTime") or 0
        if update_time > best_time:
            best, best_time = geo, update_time
    return best or (geos[0] if geos else {})


def extract_lat_lon(geo: dict) -> tuple[float | None, float | None]:
    def _f(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return _f(geo.get("latitude")), _f(geo.get("longitude"))


class SamsungTag2Coordinator(DataUpdateCoordinator):
    """Fetch token health + per-tag state from the sidecar every 60s."""

    def __init__(self, hass, client: SidecarClient,
                 update_interval_s: int = UPDATE_INTERVAL_S):
        self.client = client
        super().__init__(
            hass, _LOGGER, name=DOMAIN,
            update_interval=timedelta(seconds=update_interval_s),
        )

    async def _async_update_data(self):
        # 1) token health — the gate for everything else
        try:
            jwt = await self.hass.async_add_executor_job(self.client.get_jwt)
        except SidecarStale as err:
            _LOGGER.warning("token bank stale (%s) — entities will go unavailable",
                            err.reason)
            return {
                "token_health": {
                    "available": True,   # sidecar answered
                    "fresh": False,
                    "reason": err.reason,
                    "sha12": None, "family": None, "ttl_min": None, "bank_ts": None,
                },
                "tags": {},
            }
        except SidecarConnectionError as err:
            raise UpdateFailed(str(err)) from err
        except SidecarError as err:
            raise UpdateFailed(str(err)) from err

        token_health = {
            "available": True,
            "fresh": True,
            "reason": None,
            "sha12": jwt.get("sha12"),
            "family": jwt.get("family"),
            "ttl_min": jwt.get("ttl_min"),
            "bank_ts": jwt.get("bank_ts"),
        }

        # 2) tag list
        try:
            devices = await self.hass.async_add_executor_job(self.client.get_tags)
        except SidecarStale as err:
            token_health["fresh"] = False
            token_health["reason"] = err.reason
            return {"token_health": token_health, "tags": {}}
        except SidecarConnectionError as err:
            raise UpdateFailed(str(err)) from err
        except SidecarError as err:
            raise UpdateFailed(str(err)) from err

        # 3) per-tag state
        tags: dict[str, dict] = {}
        for device in devices:
            device_id = device.get("deviceId") or ""
            if not device_id:
                continue
            name = self._device_name(device)
            state = {
                "device_id": device_id,
                "name": name,
                "update_success": False,
                "location_found": False,
                "latitude": None,
                "longitude": None,
                "accuracy": None,
                "gps_date": None,
                "battery": parse_battery(
                    (device.get("metadata") or {}).get("battery", {}).get("level")),
            }
            try:
                geo_payload = await self.hass.async_add_executor_job(
                    self.client.get_geolocation, device_id)
            except SidecarStale as err:
                token_health["fresh"] = False
                token_health["reason"] = err.reason
                tags[device_id] = state
                continue
            except (SidecarConnectionError, Exception) as err:  # noqa: BLE001
                _LOGGER.debug("geolocation failed for %s: %r", device_id, err)
                tags[device_id] = state
                continue

            geo = pick_geolocation(geo_payload)
            lat, lon = extract_lat_lon(geo)
            if lat is not None and lon is not None:
                state["location_found"] = True
                state["latitude"] = lat
                state["longitude"] = lon
                try:
                    state["accuracy"] = float(geo.get("accuracy")) if geo.get("accuracy") is not None else None
                except (TypeError, ValueError):
                    state["accuracy"] = None
                if geo.get("lastUpdateTime"):
                    state["gps_date"] = geo["lastUpdateTime"]
            geo_battery = parse_battery(geo.get("battery"))
            if geo_battery is not None:
                state["battery"] = geo_battery
            state["update_success"] = True
            tags[device_id] = state

        return {"token_health": token_health, "tags": tags}

    @staticmethod
    def _device_name(device: dict) -> str:
        metadata = device.get("metadata") or {}
        model = (metadata.get("vendor") or {}).get("modelName")
        if model:
            return model
        identifier = device.get("identifier")
        if identifier:
            return str(identifier)
        return f"SmartTag {str(device.get('deviceId', ''))[:8]}"
