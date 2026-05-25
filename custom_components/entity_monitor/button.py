"""Button platform for the Entity Monitor integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .monitor import EntityMonitor


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Entity Monitor buttons."""
    monitor: EntityMonitor = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EntityMonitorTestNotificationButton(monitor)])


class EntityMonitorTestNotificationButton(ButtonEntity):
    """Press to send a test notification through the configured service."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "test_notification"
    _attr_icon = "mdi:bell-ring"

    def __init__(self, monitor: EntityMonitor) -> None:
        """Initialise the button."""
        self._monitor = monitor
        self._attr_unique_id = f"{monitor.entry.entry_id}_test_notification"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, monitor.entry.entry_id)},
            name="Entity Monitor",
            manufacturer="Entity Monitor",
        )

    async def async_press(self) -> None:
        """Handle the press by firing a test notification."""
        self._monitor.async_send_test_notification()
