"""Services for the Entity Monitor integration."""

from __future__ import annotations

import logging

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)

from .const import (
    DOMAIN,
    EVENT_REPORT,
    SERVICE_GENERATE_REPORT,
    SERVICE_RESET_STATISTICS,
    SERVICE_TEST_NOTIFICATION,
)

_LOGGER = logging.getLogger(__name__)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration services (idempotent)."""

    async def _generate_report(call: ServiceCall) -> ServiceResponse:
        """Build a downtime report for every Entity Monitor instance."""
        reports: dict[str, dict] = {}
        for entry_id, monitor in hass.data.get(DOMAIN, {}).items():
            report = monitor.build_report()
            reports[entry_id] = report
            hass.bus.async_fire(EVENT_REPORT, report)
            _LOGGER.info("Entity Monitor report generated: %s", report)

        if not call.return_response:
            return None
        if len(reports) == 1:
            return next(iter(reports.values()))
        return {"reports": reports}

    async def _reset_statistics(call: ServiceCall) -> None:
        """Clear stored statistics for every Entity Monitor instance."""
        for monitor in hass.data.get(DOMAIN, {}).values():
            monitor.async_reset_statistics()

    async def _test_notification(call: ServiceCall) -> None:
        """Send a sample notification through every configured monitor."""
        for monitor in hass.data.get(DOMAIN, {}).values():
            monitor.async_send_test_notification()

    if not hass.services.has_service(DOMAIN, SERVICE_GENERATE_REPORT):
        hass.services.async_register(
            DOMAIN,
            SERVICE_GENERATE_REPORT,
            _generate_report,
            supports_response=SupportsResponse.OPTIONAL,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_RESET_STATISTICS):
        hass.services.async_register(
            DOMAIN, SERVICE_RESET_STATISTICS, _reset_statistics
        )

    if not hass.services.has_service(DOMAIN, SERVICE_TEST_NOTIFICATION):
        hass.services.async_register(
            DOMAIN, SERVICE_TEST_NOTIFICATION, _test_notification
        )


@callback
def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the integration services."""
    hass.services.async_remove(DOMAIN, SERVICE_GENERATE_REPORT)
    hass.services.async_remove(DOMAIN, SERVICE_RESET_STATISTICS)
    hass.services.async_remove(DOMAIN, SERVICE_TEST_NOTIFICATION)
