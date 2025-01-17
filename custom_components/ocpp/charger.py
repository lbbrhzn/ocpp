"""Charger platform for ocpp."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, Entity, EntityDescription

from .api import CentralSystem
from .const import (
    PLATFORMS,
    CONF_CPIDS,
    DOMAIN,
    ICON,
)

CHARGER_DOMAIN = "charger"


@dataclass
class OcppChargerDescription(EntityDescription):
    """Class to describe a Charger entity."""

    pass


async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the charger platform."""

    central_system = hass.data[DOMAIN][entry.entry_id]
    cpid = list(entry.data[CONF_CPIDS][0].keys())[-1]
    cp_id = central_system.get(cpid)

    entity_desc = OcppChargerDescription(
        key=cpid,
        name=cp_id,
    )
    entity = OcppCharger(hass, central_system, cpid, entity_desc)

    async_add_devices(entity, False)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)


class OcppCharger(Entity):
    """Individual entity for a charger."""

    _attr_has_entity_name = True
    entity_description: OcppChargerDescription

    def __init__(
        self,
        hass: HomeAssistant,
        central_system: CentralSystem,
        cpid: str,
        description: OcppChargerDescription,
    ):
        """Initialize a Number instance."""
        self.cpid = cpid
        self.cp_id = description.name
        self._hass = hass
        self.central_system = central_system
        self.entity_description = description
        self._attr_unique_id = ".".join([CHARGER_DOMAIN, cpid, description.name])
        self._attr_name = description.name
        self._attr_device_info = DeviceInfo(
            name=cpid,
            identifiers={(DOMAIN, cpid), (DOMAIN, self.cp_id)},
            via_device=(DOMAIN, central_system.id),
        )
        self._attr_should_poll = True
        self._attr_available = True
        self._attr_icon = ICON

    @property
    def available(self) -> bool:
        """Return charger availability."""
        return self.central_system.get_available(self.cpid)

    @property
    def state(self) -> str | None:
        """Return charger state."""
        return self.central_system.charge_points[self.cp_id].state
