"""Adds config flow for ocpp."""

from typing import Any
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    CONN_CLASS_LOCAL_PUSH,
)
import voluptuous as vol

from .const import (
    CONF_CPID,
    CONF_CPIDS,
    CONF_CSID,
    CONF_FORCE_SMART_CHARGING,
    CONF_HOST,
    CONF_IDLE_INTERVAL,
    CONF_MAX_CURRENT,
    CONF_METER_INTERVAL,
    CONF_MONITORED_VARIABLES,
    CONF_MONITORED_VARIABLES_AUTOCONFIG,
    CONF_NUM_CONNECTORS,
    CONF_PORT,
    CONF_SKIP_SCHEMA_VALIDATION,
    CONF_SSL,
    CONF_SSL_CERTFILE_PATH,
    CONF_SSL_KEYFILE_PATH,
    CONF_WEBSOCKET_CLOSE_TIMEOUT,
    CONF_WEBSOCKET_PING_INTERVAL,
    CONF_WEBSOCKET_PING_TIMEOUT,
    CONF_WEBSOCKET_PING_TRIES,
    DEFAULT_CPID,
    DEFAULT_CSID,
    DEFAULT_FORCE_SMART_CHARGING,
    DEFAULT_HOST,
    DEFAULT_IDLE_INTERVAL,
    DEFAULT_MAX_CURRENT,
    DEFAULT_MEASURAND,
    DEFAULT_METER_INTERVAL,
    DEFAULT_MONITORED_VARIABLES,
    DEFAULT_MONITORED_VARIABLES_AUTOCONFIG,
    DEFAULT_NUM_CONNECTORS,
    DEFAULT_PORT,
    DEFAULT_SKIP_SCHEMA_VALIDATION,
    DEFAULT_SSL,
    DEFAULT_SSL_CERTFILE_PATH,
    DEFAULT_SSL_KEYFILE_PATH,
    DEFAULT_WEBSOCKET_CLOSE_TIMEOUT,
    DEFAULT_WEBSOCKET_PING_INTERVAL,
    DEFAULT_WEBSOCKET_PING_TIMEOUT,
    DEFAULT_WEBSOCKET_PING_TRIES,
    DOMAIN,
    MEASURANDS,
)

STEP_USER_CS_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_SSL, default=DEFAULT_SSL): bool,
        vol.Required(CONF_SSL_CERTFILE_PATH, default=DEFAULT_SSL_CERTFILE_PATH): str,
        vol.Required(CONF_SSL_KEYFILE_PATH, default=DEFAULT_SSL_KEYFILE_PATH): str,
        vol.Required(CONF_CSID, default=DEFAULT_CSID): str,
        vol.Required(
            CONF_WEBSOCKET_CLOSE_TIMEOUT, default=DEFAULT_WEBSOCKET_CLOSE_TIMEOUT
        ): int,
        vol.Required(
            CONF_WEBSOCKET_PING_TRIES, default=DEFAULT_WEBSOCKET_PING_TRIES
        ): int,
        vol.Required(
            CONF_WEBSOCKET_PING_INTERVAL, default=DEFAULT_WEBSOCKET_PING_INTERVAL
        ): int,
        vol.Required(
            CONF_WEBSOCKET_PING_TIMEOUT, default=DEFAULT_WEBSOCKET_PING_TIMEOUT
        ): int,
    }
)

STEP_USER_CP_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CPID, default=DEFAULT_CPID): str,
        vol.Required(CONF_MAX_CURRENT, default=DEFAULT_MAX_CURRENT): int,
        vol.Required(
            CONF_MONITORED_VARIABLES_AUTOCONFIG,
            default=DEFAULT_MONITORED_VARIABLES_AUTOCONFIG,
        ): bool,
        vol.Required(CONF_METER_INTERVAL, default=DEFAULT_METER_INTERVAL): int,
        vol.Required(CONF_IDLE_INTERVAL, default=DEFAULT_IDLE_INTERVAL): int,
        vol.Required(
            CONF_SKIP_SCHEMA_VALIDATION, default=DEFAULT_SKIP_SCHEMA_VALIDATION
        ): bool,
        vol.Required(
            CONF_FORCE_SMART_CHARGING, default=DEFAULT_FORCE_SMART_CHARGING
        ): bool,
    }
)

STEP_USER_MEASURANDS_SCHEMA = vol.Schema(
    {
        vol.Required(m, default=(True if m == DEFAULT_MEASURAND else False)): bool
        for m in MEASURANDS
    }
)


class ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OCPP."""

    VERSION = 2
    MINOR_VERSION = 1
    CONNECTION_CLASS = CONN_CLASS_LOCAL_PUSH

    def __init__(self):
        """Initialize."""
        self._data: dict[str, Any] = {}
        self._cp_id: str
        self._entry: ConfigEntry
        self._measurands: str = ""
        self._detected_num_connectors: int = DEFAULT_NUM_CONNECTORS

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        """Handle user central system initiated configuration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Don't allow servers to use same websocket port
            self._async_abort_entries_match({CONF_PORT: user_input[CONF_PORT]})
            self._data = user_input
            # Add placeholder for cpid settings
            self._data[CONF_CPIDS] = []
            return self.async_create_entry(title=self._data[CONF_CSID], data=self._data)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_CS_DATA_SCHEMA, errors=errors
        )

    async def async_step_integration_discovery(
        self, discovery_info=None
    ) -> ConfigFlowResult:
        """Handle charger discovery initiated configuration."""

        self._entry = discovery_info["entry"]
        self._cp_id = discovery_info["cp_id"]
        self._data = {**self._entry.data}

        self._detected_num_connectors = discovery_info.get(
            CONF_NUM_CONNECTORS, DEFAULT_NUM_CONNECTORS
        )

        await self.async_set_unique_id(self._cp_id)
        # Abort the flow if a config entry with the same unique ID exists
        self._abort_if_unique_id_configured()
        return await self.async_step_cp_user()

    async def async_step_cp_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure charger by user."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Don't allow duplicate cpids to be used
            self._async_abort_entries_match({CONF_CPID: user_input[CONF_CPID]})

            cp_data = {
                **user_input,
                CONF_NUM_CONNECTORS: self._detected_num_connectors,
            }
            cpids_list = self._data.get(CONF_CPIDS, []).copy()
            cpids_list.append({self._cp_id: cp_data})
            self._data = {**self._data, CONF_CPIDS: cpids_list}

            if user_input[CONF_MONITORED_VARIABLES_AUTOCONFIG]:
                self._data[CONF_CPIDS][-1][self._cp_id][CONF_MONITORED_VARIABLES] = (
                    DEFAULT_MONITORED_VARIABLES
                )
                self.hass.config_entries.async_update_entry(
                    self._entry, data=self._data
                )
                return self.async_abort(reason="Added/Updated charge point")

            else:
                return await self.async_step_measurands()

        return self.async_show_form(
            step_id="cp_user", data_schema=STEP_USER_CP_DATA_SCHEMA, errors=errors
        )

    async def async_step_measurands(self, user_input=None):
        """Select the measurands to be shown."""

        errors: dict[str, str] = {}
        if user_input is not None:
            selected_measurands = [m for m, value in user_input.items() if value]
            if not set(selected_measurands).issubset(set(MEASURANDS)):
                errors["base"] = "no_measurands_selected"
                return self.async_show_form(
                    step_id="measurands",
                    data_schema=STEP_USER_MEASURANDS_SCHEMA,
                    errors=errors,
                )
            else:
                self._measurands = ",".join(selected_measurands)
                self._data[CONF_CPIDS][-1][self._cp_id][CONF_MONITORED_VARIABLES] = (
                    self._measurands
                )

                self.hass.config_entries.async_update_entry(
                    self._entry, data=self._data
                )
                return self.async_abort(reason="Added/Updated charge point")

        return self.async_show_form(
            step_id="measurands",
            data_schema=STEP_USER_MEASURANDS_SCHEMA,
            errors=errors,
        )
