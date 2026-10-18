"""Constants for the Samsung Tag2 sidecar integration."""
DOMAIN = "samsungtag2"

# Config entry
CONF_SIDECAR_URL = "sidecar_url"
DEFAULT_SIDECAR_URL = "http://192.168.0.125:8788"

# Coordinator
UPDATE_INTERVAL_S = 60  # polls sidecar /jwt + /tags; sidecar caches upstream

# Samsung battery enum → estimated %, matching the Samsung Wearable app's
# semantics (verified live 2026-10-18: the /tag/{uuid} detail payload's
# item.battery is this enum; the /tags list endpoint reported FULL for
# every tag, which is wrong for the MEDIUM ones). ONE table is used for
# every battery read (list metadata fallback, detail payload, geolocation)
# so the same enum can never map to two different percentages.
BATTERY_LEVELS = {
    "FULL": 100,
    "MEDIUM": 60,
    "LOW": 25,
    "VERY_LOW": 5,
}
