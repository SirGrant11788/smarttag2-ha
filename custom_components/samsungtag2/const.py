"""Constants for the Samsung Tag2 sidecar integration."""
DOMAIN = "samsungtag2"

# Config entry
CONF_SIDECAR_URL = "sidecar_url"
DEFAULT_SIDECAR_URL = "http://192.168.0.125:8788"

# Coordinator
UPDATE_INTERVAL_S = 60  # polls sidecar /jwt + /tags; sidecar caches upstream

# The sidecar never exposes these, but if a stale Samsung battery string
# sneaks through, map it the same way the upstream integration does.
BATTERY_LEVELS = {
    "FULL": 100,
    "MEDIUM": 50,
    "LOW": 15,
    "VERY_LOW": 5,
}
