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
CONF_N3_MINUTES_THRESHOLD = "n3_minutes_threshold"
CONF_REPORT_TIME_HOUR = "report_time_hour"
CONF_NOTIFY_SERVICE = "notify_service"
CONF_AUTO_RESET_DAYS = "auto_reset_days"

# Envio para o HA central (Home360 Feedback Central) — opcional.
CONF_CENTRAL_URL = "central_url"
CONF_CENTRAL_CLIENT_ID = "central_client_id"
CONF_CENTRAL_TOKEN = "central_token"
CENTRAL_TOKEN_HEADER = "X-Home360-Token"
CENTRAL_SEND_TIMEOUT = 15

# Defaults
DEFAULT_NAME = "Entity Monitor"
DEFAULT_SECONDS_THRESHOLD = 30
DEFAULT_COALESCE_SECONDS = 20
DEFAULT_N3_MINUTES_THRESHOLD = 30
DEFAULT_REPORT_TIME_HOUR = 9
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

# Alert levels for per-entity unavailable events
LEVEL_SECONDS = "seconds"

# Notification kinds (for entity_monitor_notification events)
NOTIFY_N1 = "n1"
NOTIFY_N2 = "n2"
NOTIFY_N3 = "n3"
NOTIFY_TEST = "test"
NOTIFY_STATE = "estado"  # estado completo do cliente, sincronizado ao central

# Sincronização do estado completo com o central.
# Intervalo fixo; cada cliente cai num minuto próprio dentro do intervalo
# (derivado do client_id) para os clientes não enviarem todos juntos.
STATE_SYNC_INTERVAL_SECONDS = 1800
# Envio imediato após queda/recuperação: espera este tempo (agrupa eventos)...
STATE_SYNC_DEBOUNCE_SECONDS = 60
# ...e respeita um intervalo mínimo entre envios (entidade instável não inunda).
STATE_SYNC_MIN_GAP_SECONDS = 300
# Primeiro envio após o boot (depois da carência de reinício).
STATE_SYNC_BOOT_DELAY_SECONDS = 90
# Máximo de entidades detalhadas por envio (as piores); os totais por
# integração sempre consideram todas.
STATE_MAX_ENTITIES = 30
# Janelas corridas (a partir de agora).
WINDOW_DAY_SECONDS = 24 * 3600
WINDOW_WEEK_SECONDS = 7 * 24 * 3600

# Carência (segundos) após o boot do HA: quedas que se recuperam nesse período
# são transientes de reinício — não contam como queda nem como flicker.
RESTART_GRACE_SECONDS = 60

# Retenção (dias) do histórico de quedas usado para as janelas dia/semana.
# Precisa cobrir a janela de 7 dias com uma folga.
HISTORY_RETENTION_DAYS = 9

# Notification scope (number of entities involved)
SCOPE_ENTITY = "entity"  # exactly one
SCOPE_INTEGRATION = "integration"  # two or more

# Per-integration cycle states
STATE_QUIET = "quiet"
STATE_ACTIVE_TODAY = "active_today"

# Dispatcher signal used to refresh sensors
SIGNAL_UPDATE = "entity_monitor_update"

# Services
SERVICE_GENERATE_REPORT = "generate_report"
SERVICE_RESET_STATISTICS = "reset_statistics"
SERVICE_RESET_ALL = "reset_all"
SERVICE_TEST_NOTIFICATION = "test_notification"

# Storage
STORAGE_VERSION = 1
