"""Custom integration for Chargers that support the Open Charge Point Protocol."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Config, HomeAssistant
from homeassistant.helpers import device_registry
import homeassistant.helpers.config_validation as cv
import voluptuous as vol

from ocpp.v16.enums import AuthorizationStatus

from .api import CentralSystem
from .const import (
    CONF_AUTH_LIST,
    CONF_AUTH_STATUS,
    CONF_CPID,
    CONF_CSID,
    CONF_DEFAULT_AUTH_STATUS,
    CONF_ID_TAG,
    CONF_NAME,
    CONFIG,
    DEFAULT_CPID,
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


async def async_setup(hass: HomeAssistant, config: Config):
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

    """ Create Charge Point Device """
    dr.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.data.get(CONF_CPID, DEFAULT_CPID))},
        name=entry.data.get(CONF_CPID, DEFAULT_CPID),
        model="Unknown",
        via_device=((DOMAIN), central_sys.id),
    )

    hass.data[DOMAIN][entry.entry_id] = central_sys

    for platform in PLATFORMS:
        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(entry, platform)
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    central_sys = hass.data[DOMAIN][entry.entry_id]

    central_sys._server.close()
    await central_sys._server.wait_closed()

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
