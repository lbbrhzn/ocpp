"""Custom integration for Chargers that support the Open Charge Point Protocol."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers import device_registry
import homeassistant.helpers.config_validation as cv
import voluptuous as vol

from ocpp.v16.enums import AuthorizationStatus

from .api import CentralSystem
from .const import (
    CONF_AUTH_LIST,
    CONF_AUTH_STATUS,
    CONF_CPIDS,
    CONF_DEFAULT_AUTH_STATUS,
    CONF_ID_TAG,
    CONF_NAME,
    CONF_CPID,
    CONF_IDLE_INTERVAL,
    CONF_MAX_CURRENT,
    CONF_METER_INTERVAL,
    CONF_MONITORED_VARIABLES,
    CONF_MONITORED_VARIABLES_AUTOCONFIG,
    CONF_SKIP_SCHEMA_VALIDATION,
    CONF_FORCE_SMART_CHARGING,
    CONF_HOST,
    CONF_PORT,
    CONF_CSID,
    CONF_SSL,
    CONF_SSL_CERTFILE_PATH,
    CONF_SSL_KEYFILE_PATH,
    CONF_WEBSOCKET_CLOSE_TIMEOUT,
    CONF_WEBSOCKET_PING_TRIES,
    CONF_WEBSOCKET_PING_INTERVAL,
    CONF_WEBSOCKET_PING_TIMEOUT,
    CONFIG,
    DEFAULT_CSID,
    DOMAIN,
    PLATFORMS,
)

_LOGGER: logging.Logger = logging.getLogger(__package__)
logging.getLogger(DOMAIN).setLevel(logging.INFO)

AUTH_LIST_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ID_TAG): cv.string,
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional(CONF_AUTH_STATUS): cv.string,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(
            CONF_DEFAULT_AUTH_STATUS, default=AuthorizationStatus.accepted.value
        ): cv.string,
        vol.Optional(CONF_AUTH_LIST, default={}): vol.Schema(
            {cv.string: AUTH_LIST_SCHEMA}
        ),
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType):
    """Read configuration from yaml."""

    ocpp_config = config.get(DOMAIN, {})
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    hass.data[DOMAIN][CONFIG] = ocpp_config
    _LOGGER.info(f"config = {ocpp_config}")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up this integration from config entry."""
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
        _LOGGER.info(entry.data)

    central_sys = await CentralSystem.create(hass, entry)

    dr = device_registry.async_get(hass)

    """ Create Central System Device """
    dr.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.data.get(CONF_CSID, DEFAULT_CSID))},
        name=entry.data.get(CONF_CSID, DEFAULT_CSID),
        model="OCPP Central System",
    )

    """ Create first Charge Point Device """
    cpid = list(entry.data[CONF_CPIDS][0].keys())[0]
    dr.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, cpid)},
        name=cpid,
        model="Unknown",
        via_device=(DOMAIN, central_sys.id),
    )

    hass.data[DOMAIN][entry.entry_id] = central_sys

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_migrate_entry(hass, config_entry: ConfigEntry):
    """Migrate old entry."""
    _LOGGER.debug(
        "Migrating configuration from version %s.%s",
        config_entry.version,
        config_entry.minor_version,
    )

    if config_entry.version > 1:
        # This means the user has downgraded from a future version
        return False

    if config_entry.version == 1:
        old_data = {**config_entry.data}
        csid_data = {}
        cpid_data = {}
        cpid_keys = [
            CONF_CPID,
            CONF_IDLE_INTERVAL,
            CONF_MAX_CURRENT,
            CONF_METER_INTERVAL,
            CONF_MONITORED_VARIABLES,
            CONF_MONITORED_VARIABLES_AUTOCONFIG,
            CONF_SKIP_SCHEMA_VALIDATION,
            CONF_FORCE_SMART_CHARGING,
        ]
        csid_keys = [
            CONF_HOST,
            CONF_PORT,
            CONF_CSID,
            CONF_SSL,
            CONF_SSL_CERTFILE_PATH,
            CONF_SSL_KEYFILE_PATH,
            CONF_WEBSOCKET_CLOSE_TIMEOUT,
            CONF_WEBSOCKET_PING_TRIES,
            CONF_WEBSOCKET_PING_INTERVAL,
            CONF_WEBSOCKET_PING_TIMEOUT,
        ]
        for key in cpid_keys:
            cpid_data.update({key: old_data[key]})

        for key in csid_keys:
            csid_data.update({key: old_data[key]})

        # csid_data[CONF_CPIDS].append(cpid_data)
        new_data = csid_data
        new_data.update({CONF_CPIDS: [{cpid_data[CONF_CPID]: cpid_data}]})

        hass.config_entries.async_update_entry(
            config_entry, data=new_data, minor_version=0, version=2
        )

    _LOGGER.debug(
        "Migration to configuration version %s.%s successful",
        config_entry.version,
        config_entry.minor_version,
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    unloaded = False
    if DOMAIN in hass.data:
        if entry.entry_id in hass.data[DOMAIN]:
            central_sys = hass.data[DOMAIN][entry.entry_id]
            central_sys._server.close()
            await central_sys._server.wait_closed()
            unloaded = await hass.config_entries.async_unload_platforms(
                entry, PLATFORMS
            )
            if unloaded:
                hass.data[DOMAIN].pop(entry.entry_id)

    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
