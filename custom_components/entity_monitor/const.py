"""Constants for the Entity Monitor integration."""

from __future__ import annotations

DOMAIN = "entity_monitor"
PLATFORMS = ["binary_sensor", "button", "sensor"]

# Configuration keys
CONF_ENTITIES = "entities"
CONF_INTEGRATIONS = "integrations"
CONF_ONLY_PRIMARY = "only_primary_entity"
CONF_SECONDS_THRESHOLD = "seconds_threshold"
CONF_SUSTAINED_OUTAGE_SHORT_MINUTES = "sustained_outage_short_minutes"
CONF_SUSTAINED_OUTAGE_LONG_HOURS = "sustained_outage_long_hours"
CONF_NOTIFY_SERVICE = "notify_service"
CONF_NOTIFY_COOLDOWN_HOURS = "notify_cooldown_hours"
CONF_NOTIFY_SHORT_SUMMARY_HOURS = "notify_short_summary_hours"
CONF_COALESCE_SECONDS = "coalesce_seconds"
CONF_AUTO_RESET_DAYS = "auto_reset_days"

# Defaults
DEFAULT_NAME = "Entity Monitor"
DEFAULT_SECONDS_THRESHOLD = 30
DEFAULT_SUSTAINED_OUTAGE_SHORT_MINUTES = 30
DEFAULT_SUSTAINED_OUTAGE_LONG_HOURS = 12
DEFAULT_NOTIFY_COOLDOWN_HOURS = 12
DEFAULT_NOTIFY_SHORT_SUMMARY_HOURS = 2
DEFAULT_COALESCE_SECONDS = 20
DEFAULT_ONLY_PRIMARY = True
DEFAULT_AUTO_RESET_DAYS = 30

# Domains preferred when picking the "primary" entity of a device.
# Order matters: earlier = higher priority.
PRIMARY_DOMAIN_ORDER = (
    "light",
    "switch",
    "climate",
    "lock",
    "cover",
    "fan",
    "vacuum",
    "media_player",
    "humidifier",
    "water_heater",
    "alarm_control_panel",
    "camera",
    "remote",
    "binary_sensor",
    "sensor",
    "number",
    "select",
)

# Events fired on the Home Assistant bus
EVENT_UNAVAILABLE = "entity_monitor_unavailable"
EVENT_RECOVERED = "entity_monitor_recovered"
EVENT_REPORT = "entity_monitor_report"
EVENT_NOTIFICATION = "entity_monitor_notification"

# Alert levels (per-entity unavailable events)
LEVEL_SECONDS = "seconds"
LEVEL_MINUTES = "minutes"
LEVEL_HOURS = "hours"

# Notification kinds (for entity_monitor_notification events)
NOTIFY_N1 = "n1"
NOTIFY_N1_UPGRADE = "n1_upgrade"
NOTIFY_N2_SHORT = "n2_short"
NOTIFY_N2_LONG = "n2_long"
NOTIFY_N3_SHORT = "n3_short"
NOTIFY_N3_LONG = "n3_long"
NOTIFY_TEST = "test"

# Notification scope
SCOPE_ENTITY = "entity"
SCOPE_INTEGRATION = "integration"

# Dispatcher signal used to refresh sensors
SIGNAL_UPDATE = "entity_monitor_update"

# Services
SERVICE_GENERATE_REPORT = "generate_report"
SERVICE_RESET_STATISTICS = "reset_statistics"
SERVICE_TEST_NOTIFICATION = "test_notification"

# Storage
STORAGE_VERSION = 1
