"""Binary sensor platform for the Entity Monitor integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_UPDATE
from .monitor import EntityMonitor


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Entity Monitor binary sensor."""
    monitor: EntityMonitor = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EntityMonitorProblemSensor(monitor)])


class EntityMonitorProblemSensor(BinarySensorEntity):
    """Turns on while any monitored entity is currently unavailable."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Problem"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, monitor: EntityMonitor) -> None:
        """Initialise the sensor."""
        self._monitor = monitor
        self._attr_unique_id = f"{monitor.entry.entry_id}_problem"
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

    @property
    def is_on(self) -> bool:
        """Return True while at least one entity is unavailable."""
        return bool(self._monitor.ongoing_entities)

    @property
    def extra_state_attributes(self) -> dict:
        """Expose the currently unavailable entities."""
        return {
            "unavailable_entities": self._monitor.ongoing_entities,
            "unavailable_count": len(self._monitor.ongoing_entities),
            "monitored_entities": len(self._monitor.entities),
        }
