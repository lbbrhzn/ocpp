"""Device registry helpers shared by the entity platforms."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN


def connector_device_info(central_system, cpid: str, connector_id: int) -> DeviceInfo:
    """Return the device info of a connector, linked to its charger device.

    Home Assistant deprecated linking devices by an identifier tuple
    (``via_device``, removed in 2027.8): the link is made by device id, so the
    charger device registered at setup is looked up in this config entry. When
    it cannot be found the connector device is created without a link rather
    than failing.
    """
    info = DeviceInfo(
        identifiers={(DOMAIN, f"{cpid}-conn{connector_id}")},
        name=f"{cpid} Connector {connector_id}",
    )
    hass = getattr(central_system, "hass", None)
    entry = getattr(central_system, "entry", None)
    entry_id = getattr(entry, "entry_id", None)
    if not isinstance(hass, HomeAssistant) or not isinstance(entry_id, str):
        return info
    charger = next(
        iter(
            device_registry.async_get(hass).async_get_devices(
                identifiers={(DOMAIN, cpid)}, config_entry_id=entry_id
            )
        ),
        None,
    )
    if charger is not None:
        info["via_device_id"] = charger.id
    return info
