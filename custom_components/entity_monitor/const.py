"""Constants for the Entity Monitor integration."""

from __future__ import annotations

DOMAIN = "entity_monitor"
PLATFORMS = ["binary_sensor", "button", "sensor"]

# Configuration keys
CONF_ENTITIES = "entities"
CONF_INTEGRATIONS = "integrations"
CONF_ONLY_PRIMARY = "only_primary_entity"
CONF_EXCLUDED_ENTITIES = "excluded_entities"
CONF_SECONDS_THRESHOLD = "seconds_threshold"
CONF_COALESCE_SECONDS = "coalesce_seconds"
CONF_N1_BURST_WINDOW_MINUTES = "n1_burst_window_minutes"
CONF_N3_MINUTES_THRESHOLD = "n3_minutes_threshold"
CONF_REPORT_TIME_HOUR = "report_time_hour"
CONF_NOTIFY_SERVICE = "notify_service"
CONF_AUTO_RESET_DAYS = "auto_reset_days"

# Defaults
DEFAULT_NAME = "Entity Monitor"
DEFAULT_SECONDS_THRESHOLD = 30
DEFAULT_COALESCE_SECONDS = 20
DEFAULT_N1_BURST_WINDOW_MINUTES = 30
DEFAULT_N3_MINUTES_THRESHOLD = 30
DEFAULT_REPORT_TIME_HOUR = 7
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

# Notification kinds (for entity_monitor_notification events)
NOTIFY_N1_1 = "n1_1"
NOTIFY_N1_2 = "n1_2"
NOTIFY_N2 = "n2"
NOTIFY_N3_1 = "n3_1"
NOTIFY_N3_2 = "n3_2"
NOTIFY_TEST = "test"

# Notification scope (number of entities involved)
SCOPE_ENTITY = "entity"  # exactly one
SCOPE_INTEGRATION = "integration"  # two or more

# Per-integration cycle states
STATE_QUIET = "quiet"
STATE_ACTIVE_DAY1 = "active_day1"
STATE_SILENT = "silent"

# Dispatcher signal used to refresh sensors
SIGNAL_UPDATE = "entity_monitor_update"

# Services
SERVICE_GENERATE_REPORT = "generate_report"
SERVICE_RESET_STATISTICS = "reset_statistics"
SERVICE_RESET_ALL = "reset_all"
SERVICE_TEST_NOTIFICATION = "test_notification"

# Storage
STORAGE_VERSION = 2
