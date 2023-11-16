"""Custom integration for Chargers that support the Open Charge Point Protocol."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry

from .api import CentralSystem, ChargePoint
from .const import (
    CONF_CP_ID,
    CONF_CS_ID,
    CONF_DEVICE_TYPE,
    CONF_ID_TAG,
    DEFAULT_CP_ID,
    DEFAULT_CS_ID,
    DEVICE_TYPE_CENTRAL_SYSTEM,
    DEVICE_TYPE_CHARGE_POINT,
    DEVICE_TYPE_TAG,
    DOMAIN,
    PLATFORMS,
)

_LOGGER: logging.Logger = logging.getLogger(__package__)
logging.getLogger(DOMAIN).setLevel(logging.INFO)


async def async_setup(hass, config):
    """Set up this integration from yaml."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up this integration from config entry."""
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
        hass.data[DOMAIN] = {
            DEVICE_TYPE_CENTRAL_SYSTEM: {},
            DEVICE_TYPE_CHARGE_POINT: {},
            DEVICE_TYPE_TAG: {},
        }
        _LOGGER.info(entry.data)

    dr = device_registry.async_get(hass)
    device_type = entry.data.get(CONF_DEVICE_TYPE)
    name = entry.title
    manufacturer = "OCPP"
    model = device_type
    via_device = None
    cs_id = entry.data.get(CONF_CS_ID, DEFAULT_CS_ID)
    cp_id = entry.data.get(CONF_CP_ID, DEFAULT_CP_ID)

    identifier = None

    if device_type == DEVICE_TYPE_CENTRAL_SYSTEM:
        identifier = cs_id
        central = await CentralSystem.create(hass, entry)
        hass.data[DOMAIN][DEVICE_TYPE_CENTRAL_SYSTEM][cs_id] = central

    if device_type == DEVICE_TYPE_CHARGE_POINT:
        identifier = cp_id
        charge_point = ChargePoint(cp_id, hass, entry)
        hass.data[DOMAIN][DEVICE_TYPE_CENTRAL_SYSTEM][cp_id] = charge_point
        via_device = (DOMAIN, f"{DEVICE_TYPE_CENTRAL_SYSTEM}.{cs_id}")

    if device_type == DEVICE_TYPE_TAG:
        id_tag = entry.options.get(CONF_ID_TAG)
        identifier = id_tag

    dr.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{device_type}.{identifier}")},
        name=name,
        manufacturer=manufacturer,
        model=model,
        via_device=via_device,
    )

    for platform in PLATFORMS:
        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(entry, platform)
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""

    device_type = entry.data.get(CONF_DEVICE_TYPE)

    if device_type == DEVICE_TYPE_CENTRAL_SYSTEM:
        cs_id = entry.data.get(CONF_CS_ID)
        central_sys = hass.data[DOMAIN][DEVICE_TYPE_CENTRAL_SYSTEM][cs_id]
        if central_sys:
            # TODO: fix protected member usage
            central_sys._server.close()
            await central_sys._server.wait_closed()
        unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        if unloaded:
            hass.data[DOMAIN][DEVICE_TYPE_CENTRAL_SYSTEM].pop(cs_id)
    if device_type == DEVICE_TYPE_CHARGE_POINT:
        cp_id = entry.data.get(CONF_CP_ID)
        charge_point = hass.data[DOMAIN][device_type][cp_id]
        await charge_point.stop()
        unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        if unloaded:
            hass.data[DOMAIN][DEVICE_TYPE_CHARGE_POINT].pop(cp_id)
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
