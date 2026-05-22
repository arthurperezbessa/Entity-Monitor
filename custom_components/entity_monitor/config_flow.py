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
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)

from .const import (
    CONF_ENTITIES,
    CONF_MINUTES_THRESHOLD,
    CONF_NOTIFY_SERVICE,
    CONF_RENOTIFY_HOURS,
    CONF_SECONDS_THRESHOLD,
    DEFAULT_MINUTES_THRESHOLD,
    DEFAULT_NAME,
    DEFAULT_RENOTIFY_HOURS,
    DEFAULT_SECONDS_THRESHOLD,
    DOMAIN,
)


def _build_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Return the form schema, pre-filled with the given defaults."""
    return vol.Schema(
        {
            vol.Required(
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
                CONF_MINUTES_THRESHOLD,
                default=defaults.get(
                    CONF_MINUTES_THRESHOLD, DEFAULT_MINUTES_THRESHOLD
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=1440,
                    step=1,
                    unit_of_measurement="min",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_NOTIFY_SERVICE,
                default=defaults.get(CONF_NOTIFY_SERVICE, ""),
            ): TextSelector(),
            vol.Required(
                CONF_RENOTIFY_HOURS,
                default=defaults.get(
                    CONF_RENOTIFY_HOURS, DEFAULT_RENOTIFY_HOURS
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
        }
    )


def _normalise(user_input: dict[str, Any]) -> dict[str, Any]:
    """Coerce the numeric selector values to integers."""
    return {
        CONF_ENTITIES: user_input[CONF_ENTITIES],
        CONF_SECONDS_THRESHOLD: int(user_input[CONF_SECONDS_THRESHOLD]),
        CONF_MINUTES_THRESHOLD: int(user_input[CONF_MINUTES_THRESHOLD]),
        CONF_NOTIFY_SERVICE: user_input.get(CONF_NOTIFY_SERVICE, "").strip(),
        CONF_RENOTIFY_HOURS: int(user_input[CONF_RENOTIFY_HOURS]),
    }


class EntityMonitorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial configuration of Entity Monitor."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user setup step."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return self.async_create_entry(
                title=DEFAULT_NAME, data={}, options=_normalise(user_input)
            )

        return self.async_show_form(
            step_id="user", data_schema=_build_schema({})
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
        if user_input is not None:
            return self.async_create_entry(
                title="", data=_normalise(user_input)
            )

        return self.async_show_form(
            step_id="init",
            data_schema=_build_schema(dict(self.config_entry.options)),
        )
