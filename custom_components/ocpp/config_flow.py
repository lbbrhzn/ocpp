"""Adds config flow for ocpp."""
from homeassistant import config_entries
import voluptuous as vol

from .const import (
    CONF_CPID,
    CONF_CSID,
    CONF_FORCE_SMART_CHARGING,
    CONF_HOST,
    CONF_IDLE_INTERVAL,
    CONF_MAX_CURRENT,
    CONF_METER_INTERVAL,
    CONF_MONITORED_VARIABLES,
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
    DEFAULT_METER_INTERVAL,
    DEFAULT_MONITORED_VARIABLES,
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
)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_SSL, default=DEFAULT_SSL): bool,
        vol.Required(CONF_SSL_CERTFILE_PATH, default=DEFAULT_SSL_CERTFILE_PATH): str,
        vol.Required(CONF_SSL_KEYFILE_PATH, default=DEFAULT_SSL_KEYFILE_PATH): str,
        vol.Required(CONF_CSID, default=DEFAULT_CSID): str,
        vol.Required(CONF_CPID, default=DEFAULT_CPID): str,
        vol.Required(CONF_MAX_CURRENT, default=DEFAULT_MAX_CURRENT): int,
        vol.Required(
            CONF_MONITORED_VARIABLES, default=DEFAULT_MONITORED_VARIABLES
        ): str,
        vol.Required(CONF_METER_INTERVAL, default=DEFAULT_METER_INTERVAL): int,
        vol.Required(CONF_IDLE_INTERVAL, default=DEFAULT_IDLE_INTERVAL): int,
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
        vol.Required(
            CONF_SKIP_SCHEMA_VALIDATION, default=DEFAULT_SKIP_SCHEMA_VALIDATION
        ): bool,
        vol.Required(
            CONF_FORCE_SMART_CHARGING, default=DEFAULT_FORCE_SMART_CHARGING
        ): bool,
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
            self._data[CONF_MONITORED_VARIABLES] = DEFAULT_MONITORED_VARIABLES
            return self.async_create_entry(title=self._data[CONF_CSID], data=self._data)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )
