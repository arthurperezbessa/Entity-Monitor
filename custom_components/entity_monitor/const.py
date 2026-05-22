"""Constants for the Entity Monitor integration."""

from __future__ import annotations

DOMAIN = "entity_monitor"
PLATFORMS = ["binary_sensor", "sensor"]

# Configuration keys
CONF_ENTITIES = "entities"
CONF_SECONDS_THRESHOLD = "seconds_threshold"
CONF_MINUTES_THRESHOLD = "minutes_threshold"

# Defaults
DEFAULT_NAME = "Entity Monitor"
DEFAULT_SECONDS_THRESHOLD = 30
DEFAULT_MINUTES_THRESHOLD = 5

# Events fired on the Home Assistant bus
EVENT_UNAVAILABLE = "entity_monitor_unavailable"
EVENT_RECOVERED = "entity_monitor_recovered"
EVENT_REPORT = "entity_monitor_report"

# Alert levels
LEVEL_SECONDS = "seconds"
LEVEL_MINUTES = "minutes"

# Dispatcher signal used to refresh sensors
SIGNAL_UPDATE = "entity_monitor_update"

# Services
SERVICE_GENERATE_REPORT = "generate_report"
SERVICE_RESET_STATISTICS = "reset_statistics"

# Storage
STORAGE_VERSION = 1
