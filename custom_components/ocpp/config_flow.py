"""Adds config flow for ocpp."""
from homeassistant import config_entries
import voluptuous as vol

from .const import (
    CONF_CPID,
    CONF_CSID,
    CONF_HOST,
    CONF_METER_INTERVAL,
    CONF_MONITORED_VARIABLES,
    CONF_PORT,
    DEFAULT_CPID,
    DEFAULT_CSID,
    DEFAULT_HOST,
    DEFAULT_MEASURAND,
    DEFAULT_METER_INTERVAL,
    DEFAULT_PORT,
    DOMAIN,
    MEASURANDS,
)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_CSID, default=DEFAULT_CSID): str,
        vol.Required(CONF_CPID, default=DEFAULT_CPID): str,
        vol.Required(CONF_METER_INTERVAL, default=DEFAULT_METER_INTERVAL): int,
    }
)
STEP_USER_MEASURANDS_SCHEMA = vol.Schema(
    {
        vol.Required(m, default=(True if m == DEFAULT_MEASURAND else False)): bool
        for m in MEASURANDS
    }
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OCPP."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_PUSH

    def __init__(self):
        """Initialize."""
        self._data = {}

    async def async_step_user(self, user_input=None):
        """Handle user initiated configuration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Todo: validate the user input
            self._data = user_input
            return await self.async_step_measurands()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_measurands(self, user_input=None):
        """Select the measurands to be shown."""

        errors: dict[str, str] = {}
        if user_input is not None:
            selected_measurands = [m for m, value in user_input.items() if value]
            if set(selected_measurands).issubset(set(MEASURANDS)):
                self._data[CONF_MONITORED_VARIABLES] = ",".join(selected_measurands)
                return self.async_create_entry(
                    title=self._data[CONF_CSID], data=self._data
                )
            else:
                errors["base"] = "measurand"
        return self.async_show_form(
            step_id="measurands",
            data_schema=STEP_USER_MEASURANDS_SCHEMA,
            errors=errors,
        )
