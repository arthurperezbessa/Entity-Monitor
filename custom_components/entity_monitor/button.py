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
    async_add_entities(
        [
            EntityMonitorTestNotificationButton(monitor),
            EntityMonitorResetAllButton(monitor),
        ]
    )


class _BaseButton(ButtonEntity):
    """Shared wiring for the Entity Monitor buttons."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, monitor: EntityMonitor, key: str) -> None:
        """Initialise the button."""
        self._monitor = monitor
        self._attr_unique_id = f"{monitor.entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, monitor.entry.entry_id)},
            name="Entity Monitor",
            manufacturer="Entity Monitor",
        )


class EntityMonitorTestNotificationButton(_BaseButton):
    """Press to send a test notification through the configured service."""

    _attr_translation_key = "test_notification"
    _attr_icon = "mdi:bell-ring"

    def __init__(self, monitor: EntityMonitor) -> None:
        """Initialise the button."""
        super().__init__(monitor, "test_notification")

    async def async_press(self) -> None:
        """Handle the press by firing a test notification."""
        self._monitor.async_send_test_notification()


class EntityMonitorResetAllButton(_BaseButton):
    """Press to wipe every counter, state and ongoing-outage record."""

    _attr_translation_key = "reset_all"
    _attr_icon = "mdi:delete-sweep"

    def __init__(self, monitor: EntityMonitor) -> None:
        """Initialise the button."""
        super().__init__(monitor, "reset_all")

    async def async_press(self) -> None:
        """Handle the press by zeroing everything."""
        self._monitor.async_reset_all()
