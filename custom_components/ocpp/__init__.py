"""Custom integration for Chargers that support the Open Charge Point Protocol."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import Config, HomeAssistant

from .central_system import CentralSystem
from .const import DOMAIN, PLATFORMS

_LOGGER: logging.Logger = logging.getLogger(__package__)
logging.getLogger(DOMAIN).setLevel(logging.DEBUG)


async def async_setup(hass: HomeAssistant, config: Config):
    """Set up this integration using YAML is not supported."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up this integration using UI."""
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
        _LOGGER.info(entry.data)

    cfg_host = entry.data.get(CONF_HOST)
    cfg_port = entry.data.get(CONF_PORT)

    central_sys = await CentralSystem.create(entry.data, host=cfg_host, port=cfg_port)

    hass.data[DOMAIN][entry.entry_id] = central_sys

    for platform in PLATFORMS:
        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(entry, platform)
        )

    return True
