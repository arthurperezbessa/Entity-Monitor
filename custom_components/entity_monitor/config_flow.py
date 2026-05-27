"""Config flow for the Entity Monitor integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_AUTO_RESET_DAYS,
    CONF_COALESCE_SECONDS,
    CONF_ENTITIES,
    CONF_INTEGRATIONS,
    CONF_NOTIFY_COOLDOWN_HOURS,
    CONF_NOTIFY_SERVICE,
    CONF_NOTIFY_SHORT_SUMMARY_HOURS,
    CONF_ONLY_PRIMARY,
    CONF_SECONDS_THRESHOLD,
    CONF_SUSTAINED_OUTAGE_LONG_HOURS,
    CONF_SUSTAINED_OUTAGE_SHORT_MINUTES,
    DEFAULT_AUTO_RESET_DAYS,
    DEFAULT_COALESCE_SECONDS,
    DEFAULT_NAME,
    DEFAULT_NOTIFY_COOLDOWN_HOURS,
    DEFAULT_NOTIFY_SHORT_SUMMARY_HOURS,
    DEFAULT_ONLY_PRIMARY,
    DEFAULT_SECONDS_THRESHOLD,
    DEFAULT_SUSTAINED_OUTAGE_LONG_HOURS,
    DEFAULT_SUSTAINED_OUTAGE_SHORT_MINUTES,
    DOMAIN,
)


def _available_integrations(hass: HomeAssistant) -> list[str]:
    """Return platforms that currently have entities registered."""
    registry = er.async_get(hass)
    return sorted({entry.platform for entry in registry.entities.values()})


def _build_schema(
    hass: HomeAssistant, defaults: dict[str, Any]
) -> vol.Schema:
    """Return the form schema, pre-filled with the given defaults."""
    integrations = _available_integrations(hass)
    return vol.Schema(
        {
            vol.Optional(
                CONF_INTEGRATIONS,
                default=defaults.get(CONF_INTEGRATIONS, []),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=integrations,
                    multiple=True,
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_ONLY_PRIMARY,
                default=defaults.get(
                    CONF_ONLY_PRIMARY, DEFAULT_ONLY_PRIMARY
                ),
            ): BooleanSelector(),
            vol.Optional(
                CONF_ENTITIES,
                default=defaults.get(CONF_ENTITIES, []),
            ): EntitySelector(EntitySelectorConfig(multiple=True)),
            vol.Required(
                CONF_SECONDS_THRESHOLD,
                default=defaults.get(
                    CONF_SECONDS_THRESHOLD, DEFAULT_SECONDS_THRESHOLD
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=3600,
                    step=1,
                    unit_of_measurement="s",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_COALESCE_SECONDS,
                default=defaults.get(
                    CONF_COALESCE_SECONDS, DEFAULT_COALESCE_SECONDS
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=600,
                    step=1,
                    unit_of_measurement="s",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_NOTIFY_SERVICE,
                default=defaults.get(CONF_NOTIFY_SERVICE, ""),
            ): TextSelector(),
            vol.Required(
                CONF_NOTIFY_COOLDOWN_HOURS,
                default=defaults.get(
                    CONF_NOTIFY_COOLDOWN_HOURS,
                    DEFAULT_NOTIFY_COOLDOWN_HOURS,
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=168,
                    step=1,
                    unit_of_measurement="h",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_NOTIFY_SHORT_SUMMARY_HOURS,
                default=defaults.get(
                    CONF_NOTIFY_SHORT_SUMMARY_HOURS,
                    DEFAULT_NOTIFY_SHORT_SUMMARY_HOURS,
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=168,
                    step=1,
                    unit_of_measurement="h",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_SUSTAINED_OUTAGE_SHORT_MINUTES,
                default=defaults.get(
                    CONF_SUSTAINED_OUTAGE_SHORT_MINUTES,
                    DEFAULT_SUSTAINED_OUTAGE_SHORT_MINUTES,
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=1440,
                    step=1,
                    unit_of_measurement="min",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_SUSTAINED_OUTAGE_LONG_HOURS,
                default=defaults.get(
                    CONF_SUSTAINED_OUTAGE_LONG_HOURS,
                    DEFAULT_SUSTAINED_OUTAGE_LONG_HOURS,
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=168,
                    step=1,
                    unit_of_measurement="h",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_AUTO_RESET_DAYS,
                default=defaults.get(
                    CONF_AUTO_RESET_DAYS, DEFAULT_AUTO_RESET_DAYS
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=365,
                    step=1,
                    unit_of_measurement="d",
                    mode=NumberSelectorMode.BOX,
                )
            ),
        }
    )


def _normalise(user_input: dict[str, Any]) -> dict[str, Any]:
    """Coerce the form values to their final types."""
    return {
        CONF_ENTITIES: user_input.get(CONF_ENTITIES, []),
        CONF_INTEGRATIONS: user_input.get(CONF_INTEGRATIONS, []),
        CONF_ONLY_PRIMARY: bool(
            user_input.get(CONF_ONLY_PRIMARY, DEFAULT_ONLY_PRIMARY)
        ),
        CONF_SECONDS_THRESHOLD: int(user_input[CONF_SECONDS_THRESHOLD]),
        CONF_COALESCE_SECONDS: int(user_input[CONF_COALESCE_SECONDS]),
        CONF_NOTIFY_SERVICE: user_input.get(CONF_NOTIFY_SERVICE, "").strip(),
        CONF_NOTIFY_COOLDOWN_HOURS: int(
            user_input[CONF_NOTIFY_COOLDOWN_HOURS]
        ),
        CONF_NOTIFY_SHORT_SUMMARY_HOURS: int(
            user_input[CONF_NOTIFY_SHORT_SUMMARY_HOURS]
        ),
        CONF_SUSTAINED_OUTAGE_SHORT_MINUTES: int(
            user_input[CONF_SUSTAINED_OUTAGE_SHORT_MINUTES]
        ),
        CONF_SUSTAINED_OUTAGE_LONG_HOURS: int(
            user_input[CONF_SUSTAINED_OUTAGE_LONG_HOURS]
        ),
        CONF_AUTO_RESET_DAYS: int(
            user_input.get(CONF_AUTO_RESET_DAYS, DEFAULT_AUTO_RESET_DAYS)
        ),
    }


def _validate(user_input: dict[str, Any]) -> dict[str, str]:
    """Return a dict of field→error code for any invalid input."""
    if not user_input.get(CONF_ENTITIES) and not user_input.get(
        CONF_INTEGRATIONS
    ):
        return {"base": "nothing_selected"}
    short_h = int(user_input.get(CONF_NOTIFY_SHORT_SUMMARY_HOURS, 0))
    cooldown_h = int(user_input.get(CONF_NOTIFY_COOLDOWN_HOURS, 0))
    if short_h and short_h >= cooldown_h:
        return {CONF_NOTIFY_SHORT_SUMMARY_HOURS: "short_summary_too_long"}
    return {}


class EntityMonitorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial configuration of Entity Monitor."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user setup step."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                return self.async_create_entry(
                    title=DEFAULT_NAME,
                    data={},
                    options=_normalise(user_input),
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_build_schema(self.hass, user_input or {}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> "EntityMonitorOptionsFlow":
        """Return the options flow handler."""
        return EntityMonitorOptionsFlow()


class EntityMonitorOptionsFlow(OptionsFlow):
    """Allow the monitored entities and thresholds to be edited later."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                return self.async_create_entry(
                    title="", data=_normalise(user_input)
                )

        defaults = user_input or dict(self.config_entry.options)
        return self.async_show_form(
            step_id="init",
            data_schema=_build_schema(self.hass, defaults),
            errors=errors,
        )
