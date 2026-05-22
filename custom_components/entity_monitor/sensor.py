"""Sensor platform for the Entity Monitor integration."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_UPDATE
from .monitor import EntityMonitor

# How many rows of each ranking are exposed on the report sensor.
TOP_N = 10


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Entity Monitor sensors."""
    monitor: EntityMonitor = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            TotalOutagesSensor(monitor),
            TotalDowntimeSensor(monitor),
            DowntimeReportSensor(monitor),
        ]
    )


class _BaseSensor(SensorEntity):
    """Common wiring shared by every Entity Monitor sensor."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, monitor: EntityMonitor, key: str) -> None:
        """Initialise the sensor."""
        self._monitor = monitor
        self._attr_unique_id = f"{monitor.entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, monitor.entry.entry_id)},
            name="Entity Monitor",
            manufacturer="Entity Monitor",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to monitor updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_UPDATE, self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        """Refresh the state when the monitor reports a change."""
        self.async_write_ha_state()


class TotalOutagesSensor(_BaseSensor):
    """Counts how many outages have been recorded in total."""

    _attr_name = "Total outages"
    _attr_icon = "mdi:counter"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, monitor: EntityMonitor) -> None:
        """Initialise the sensor."""
        super().__init__(monitor, "total_outages")

    @property
    def native_value(self) -> int:
        """Return the total number of recorded outages."""
        return self._monitor.total_outages


class TotalDowntimeSensor(_BaseSensor):
    """Reports the accumulated downtime across all monitored entities."""

    _attr_name = "Total downtime"
    _attr_icon = "mdi:timer-alert"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 1

    def __init__(self, monitor: EntityMonitor) -> None:
        """Initialise the sensor."""
        super().__init__(monitor, "total_downtime")

    @property
    def native_value(self) -> float:
        """Return the total downtime in minutes."""
        return round(self._monitor.total_downtime / 60, 2)


class DowntimeReportSensor(_BaseSensor):
    """Exposes the offline ranking as attributes for dashboards."""

    _attr_name = "Downtime report"
    _attr_icon = "mdi:chart-box"

    def __init__(self, monitor: EntityMonitor) -> None:
        """Initialise the sensor."""
        super().__init__(monitor, "downtime_report")

    @property
    def native_value(self) -> int:
        """Return how many entities are currently unavailable."""
        return len(self._monitor.ongoing_entities)

    @property
    def extra_state_attributes(self) -> dict:
        """Expose the ranked report, trimmed to the top entries."""
        report = self._monitor.build_report()
        return {
            "generated_at": report["generated_at"],
            "monitored_entities": report["monitored_entities"],
            "total_outages": report["total_outages"],
            "total_downtime": report["total_downtime"],
            "currently_unavailable": report["currently_unavailable"],
            "worst_entities": report["by_entity"][:TOP_N],
            "worst_integrations": report["by_integration"][:TOP_N],
        }
