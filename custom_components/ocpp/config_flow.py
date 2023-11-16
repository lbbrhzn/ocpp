"""Adds config flow for ocpp."""
import logging

from homeassistant import config_entries
from homeassistant.core import callback
import voluptuous as vol

from ocpp.v16.enums import AuthorizationStatus

from .const import (
    CONF_AUTH_STATUS,
    CONF_CP_ID,
    CONF_DEVICE_TYPE,
    CONF_FORCE_SMART_CHARGING,
    CONF_HOST,
    CONF_ID_TAG,
    CONF_IDLE_INTERVAL,
    CONF_MAX_CURRENT,
    CONF_METER_INTERVAL,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SKIP_SCHEMA_VALIDATION,
    CONF_SSL,
    CONF_SSL_CERTFILE_PATH,
    CONF_SSL_KEYFILE_PATH,
    CONF_WEBSOCKET_CLOSE_TIMEOUT,
    CONF_WEBSOCKET_PING_INTERVAL,
    CONF_WEBSOCKET_PING_TIMEOUT,
    CONF_WEBSOCKET_PING_TRIES,
    DEFAULT_AUTH_STATUS,
    DEFAULT_CP_ID,
    DEFAULT_CP_NAME,
    DEFAULT_CS_NAME,
    DEFAULT_DEVICE_TYPE,
    DEFAULT_FORCE_SMART_CHARGING,
    DEFAULT_HOST,
    DEFAULT_ID_TAG,
    DEFAULT_IDLE_INTERVAL,
    DEFAULT_MAX_CURRENT,
    DEFAULT_METER_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SKIP_SCHEMA_VALIDATION,
    DEFAULT_SSL,
    DEFAULT_SSL_CERTFILE_PATH,
    DEFAULT_SSL_KEYFILE_PATH,
    DEFAULT_TAG_NAME,
    DEFAULT_WEBSOCKET_CLOSE_TIMEOUT,
    DEFAULT_WEBSOCKET_PING_INTERVAL,
    DEFAULT_WEBSOCKET_PING_TIMEOUT,
    DEFAULT_WEBSOCKET_PING_TRIES,
    DEVICE_TYPE_CENTRAL_SYSTEM,
    DEVICE_TYPE_CHARGE_POINT,
    DEVICE_TYPE_TAG,
    DEVICE_TYPES,
    DOMAIN,
)

_LOGGER: logging.Logger = logging.getLogger(__package__)


CENTRAL_SYSTEM_SCHEMA_ITEMS: dict = {
    vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
    vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
    vol.Required(CONF_SSL, default=DEFAULT_SSL): bool,
    vol.Required(
        CONF_SSL_CERTFILE_PATH,
        default=DEFAULT_SSL_CERTFILE_PATH,
    ): str,
    vol.Required(
        CONF_SSL_KEYFILE_PATH,
        default=DEFAULT_SSL_KEYFILE_PATH,
    ): str,
    vol.Required(
        CONF_WEBSOCKET_CLOSE_TIMEOUT,
        default=DEFAULT_WEBSOCKET_CLOSE_TIMEOUT,
    ): int,
    vol.Required(
        CONF_WEBSOCKET_PING_TRIES,
        default=DEFAULT_WEBSOCKET_PING_TRIES,
    ): int,
    vol.Required(
        CONF_WEBSOCKET_PING_INTERVAL,
        default=DEFAULT_WEBSOCKET_PING_INTERVAL,
    ): int,
    vol.Required(
        CONF_WEBSOCKET_PING_TIMEOUT,
        default=DEFAULT_WEBSOCKET_PING_TIMEOUT,
    ): int,
    vol.Required(
        CONF_SKIP_SCHEMA_VALIDATION,
        default=DEFAULT_SKIP_SCHEMA_VALIDATION,
    ): bool,
}
CENTRAL_SYSTEM_SCHEMA: vol.Schema = vol.Schema(CENTRAL_SYSTEM_SCHEMA_ITEMS)

CHARGE_POINT_SCHEMA_ITEMS: dict = {
    vol.Required(CONF_CP_ID, default=DEFAULT_CP_ID): str,
    vol.Optional(
        CONF_PASSWORD,
        description={"suggested_value": ""},
    ): str,
    vol.Required(
        CONF_MAX_CURRENT,
        default=DEFAULT_MAX_CURRENT,
    ): int,
    vol.Required(
        CONF_IDLE_INTERVAL,
        default=DEFAULT_IDLE_INTERVAL,
    ): int,
    vol.Required(
        CONF_METER_INTERVAL,
        default=DEFAULT_METER_INTERVAL,
    ): int,
    vol.Required(
        CONF_FORCE_SMART_CHARGING,
        default=DEFAULT_FORCE_SMART_CHARGING,
    ): bool,
}
CHARGE_POINT_SCHEMA: vol.Schema = vol.Schema(CHARGE_POINT_SCHEMA_ITEMS)

TAG_SCHEMA_ITEMS: dict = {
    vol.Required(CONF_ID_TAG, default=DEFAULT_ID_TAG): str,
    vol.Required(CONF_AUTH_STATUS, default=DEFAULT_AUTH_STATUS): vol.In(
        [
            AuthorizationStatus.accepted,
            AuthorizationStatus.blocked,
            AuthorizationStatus.expired,
            AuthorizationStatus.invalid,
        ]
    ),
}

TAG_SCHEMA: vol.Schema = vol.Schema(TAG_SCHEMA_ITEMS)

DEVICE_SCHEMA: vol.Schema = vol.Schema(
    {vol.Required(CONF_DEVICE_TYPE, default=DEFAULT_DEVICE_TYPE): vol.In(DEVICE_TYPES)}
)


def charge_point_exists(
    cp_id: str,
    entries: config_entries.ConfigEntries,
    skip_entry: config_entries.ConfigEntry = None,
) -> bool:
    """Check whether there is another entry with the specified charge point id."""
    for entry in entries.async_entries(DOMAIN):
        if (
            (entry.data.get(CONF_DEVICE_TYPE, None) == DEVICE_TYPE_CHARGE_POINT)
            and (entry.options.get(CONF_CP_ID, None) == cp_id)
            and (entry is not skip_entry)
        ):
            return True
    return False


def tag_exists(
    id_tag,
    entries: config_entries.ConfigEntries,
    skip_entry: config_entries.ConfigEntry = None,
) -> bool:
    """Check whether there is another entry with with the specified tag id."""
    for entry in entries.async_entries(DOMAIN):
        if (
            (entry.data.get(CONF_DEVICE_TYPE, None) == DEVICE_TYPE_TAG)
            and (entry.options.get(CONF_ID_TAG) == id_tag)
            and (entry is not skip_entry)
        ):
            return True
    return False


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OCPP."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_PUSH

    async def get_central_system_entry(self):
        """Find central system entry, if present."""
        entries = self.hass.config_entries.async_entries(DOMAIN)
        for entry in entries:
            if entry.data.get(CONF_DEVICE_TYPE, None) == DEVICE_TYPE_CENTRAL_SYSTEM:
                return entry
        return None

    async def async_step_user(self, user_input: dict = None):
        """Handle user initiated configuration."""
        # If there is no central system entity, we start configurating one
        if await self.get_central_system_entry() is None:
            return await self.async_step_central_system(user_input)
        # Otherwise, we let the user select which type of device to add
        return await self.async_step_select_device(user_input)

    async def async_step_select_device(self, user_input: dict = None):
        """Handle device selection."""
        if user_input is None:
            return self.async_show_form(
                step_id="select_device",
                data_schema=DEVICE_SCHEMA,
            )
        device_type = user_input.get(CONF_DEVICE_TYPE, DEFAULT_DEVICE_TYPE)
        if device_type == DEVICE_TYPE_CENTRAL_SYSTEM:
            return await self.async_step_central_system()
        if device_type == DEVICE_TYPE_CHARGE_POINT:
            return await self.async_step_charge_point()
        if device_type == DEVICE_TYPE_TAG:
            return await self.async_step_tag()
        return self.async_show_form(
            step_id="select_device",
            data_schema=DEVICE_SCHEMA,
            errors={"CONF_DEVICE_TYPE": "not_supported"},
        )

    async def async_step_central_system(self, user_input: dict = None):
        """Handle central system configuration."""
        if user_input is None:
            return self.async_show_form(
                step_id="central_system",
                data_schema=vol.Schema(
                    {vol.Required(CONF_NAME, default=DEFAULT_CS_NAME): str}
                ).extend(CENTRAL_SYSTEM_SCHEMA_ITEMS),
            )
        options = user_input.copy()
        options.pop(CONF_NAME)
        return self.async_create_entry(
            title=user_input.get(CONF_NAME),
            data={CONF_DEVICE_TYPE: DEVICE_TYPE_CENTRAL_SYSTEM},
            options=options,
        )

    async def async_step_charge_point(self, user_input: dict = None):
        """Handle charge point configuration."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if charge_point_exists(
                user_input.get(CONF_CP_ID), self.hass.config_entries
            ):
                errors[CONF_CP_ID] = "charge_point_exists"
            if not errors:
                options = user_input.copy()
                options.pop(CONF_NAME)
                return self.async_create_entry(
                    title=user_input.get(CONF_NAME),
                    data={CONF_DEVICE_TYPE: DEVICE_TYPE_CHARGE_POINT},
                    options=options,
                )
        return self.async_show_form(
            step_id="charge_point",
            data_schema=vol.Schema(
                {vol.Required(CONF_NAME, default=DEFAULT_CP_NAME): str}
            ).extend(CHARGE_POINT_SCHEMA_ITEMS),
            errors=errors,
        )

    async def async_step_tag(self, user_input: dict = None):
        """Handle tag configuration."""
        errors: dict[str, str] = {}
        if user_input is not None:
            id_tag = user_input.get(CONF_ID_TAG, DEFAULT_ID_TAG)
            if tag_exists(id_tag, self.hass.config_entries):
                errors[CONF_ID_TAG] = "tag_exists"
            if not errors:
                options = user_input.copy()
                options.pop(CONF_NAME)
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={CONF_DEVICE_TYPE: DEVICE_TYPE_TAG},
                    options=options,
                )
        return self.async_show_form(
            step_id="tag",
            data_schema=vol.Schema(
                {vol.Required(CONF_NAME, default=DEFAULT_TAG_NAME): str}
            ).extend(TAG_SCHEMA_ITEMS),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlowWithConfigEntry):
    """Handles options flow for the component."""

    async def async_step_init(self, user_input: dict = None):
        """Handle initial step."""
        if self._config_entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_CENTRAL_SYSTEM:
            return await self.async_step_central_system(user_input)
        if self._config_entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_CHARGE_POINT:
            return await self.async_step_charge_point(user_input)
        if self._config_entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_TAG:
            return await self.async_step_tag(user_input)
        return self.async_abort(reason="not_supported")

    async def async_step_central_system(self, user_input: dict = None):
        """Handle Central System options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not errors:
                return self.async_create_entry(
                    title=self._config_entry.title,
                    data=user_input,
                )
        return self.async_show_form(
            step_id="central_system",
            data_schema=self.add_suggested_values_to_schema(
                CENTRAL_SYSTEM_SCHEMA, self._config_entry.options
            ),
            description_placeholders={
                "name": self._config_entry.title,
            },
            errors=errors,
        )

    async def async_step_charge_point(self, user_input: dict = None):
        """Handle Charge Point options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if charge_point_exists(
                user_input.get(CONF_CP_ID), self.hass.config_entries
            ):
                errors[CONF_CP_ID] = "charge_point_exists"
            if not errors:
                return self.async_create_entry(
                    title=self._config_entry.title, data=user_input
                )
        return self.async_show_form(
            step_id="charge_point",
            data_schema=self.add_suggested_values_to_schema(
                CHARGE_POINT_SCHEMA, self._config_entry.options
            ),
            description_placeholders={
                "name": self._config_entry.title,
            },
            errors=errors,
        )

    async def async_step_tag(self, user_input: dict = None):
        """Handle Tag options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            id_tag = user_input.get(CONF_ID_TAG)
            if tag_exists(id_tag, self.hass.config_entries, self._config_entry):
                errors[CONF_ID_TAG] = "tag_exists"
            if not errors:
                return self.async_create_entry(
                    title=user_input.get(CONF_NAME), data=user_input
                )
        return self.async_show_form(
            step_id="tag",
            data_schema=self.add_suggested_values_to_schema(
                TAG_SCHEMA, self._config_entry.options
            ),
            description_placeholders={
                "name": self._config_entry.title,
            },
            errors=errors,
        )
