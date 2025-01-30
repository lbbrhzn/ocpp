"""Number platform for ocpp."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from homeassistant.components.number import (
    DOMAIN as NUMBER_DOMAIN,
    NumberEntity,
    NumberEntityDescription,
    RestoreNumber,
)
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo

from .api import CentralSystem
from .const import (
    CONF_CPID,
    CONF_CPIDS,
    CONF_MAX_CURRENT,
    DATA_UPDATED,
    DEFAULT_MAX_CURRENT,
    DOMAIN,
    ICON,
)
from .enums import Profiles


@dataclass
class OcppNumberDescription(NumberEntityDescription):
    """Class to describe a Number entity."""

    initial_value: float | None = None


ELECTRIC_CURRENT_AMPERE = UnitOfElectricCurrent.AMPERE

NUMBERS: Final = [
    OcppNumberDescription(
        key="maximum_current",
        name="Maximum Current",
        icon=ICON,
        initial_value=DEFAULT_MAX_CURRENT,
        native_min_value=0,
        native_max_value=DEFAULT_MAX_CURRENT,
        native_step=1,
        native_unit_of_measurement=ELECTRIC_CURRENT_AMPERE,
    ),
]


async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the number platform."""

    central_system = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for charger in entry.data[CONF_CPIDS]:
        cp_id_settings = list(charger.values())[0]
        cpid = cp_id_settings[CONF_CPID]

        for ent in NUMBERS:
            if ent.key == "maximum_current":
                ent.initial_value = entry.data.get(
                    CONF_MAX_CURRENT, DEFAULT_MAX_CURRENT
                )
                ent.native_max_value = entry.data.get(
                    CONF_MAX_CURRENT, DEFAULT_MAX_CURRENT
                )
            cpx = ChargePointNumber(hass, central_system, cpid, ent)
            entities.append(cpx)

    async_add_devices(entities, False)


class ChargePointNumber(RestoreNumber, NumberEntity):
    """Individual slider for setting charge rate."""

    _attr_has_entity_name = True
    entity_description: OcppNumberDescription

    def __init__(
        self,
        hass: HomeAssistant,
        central_system: CentralSystem,
        cpid: str,
        description: OcppNumberDescription,
    ):
        """Initialize a Number instance."""
        self.cpid = cpid
        self._hass = hass
        self.central_system = central_system
        self.entity_description = description
        self._attr_unique_id = ".".join(
            [NUMBER_DOMAIN, self.cpid, self.entity_description.key]
        )
        self._attr_name = self.entity_description.name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.cpid)},
        )
        self._attr_native_value = self.entity_description.initial_value
        self._attr_should_poll = False
        self._attr_available = True

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        if restored := await self.async_get_last_number_data():
            self._attr_native_value = restored.native_value
        async_dispatcher_connect(
            self._hass, DATA_UPDATED, self._schedule_immediate_update
        )

    @callback
    def _schedule_immediate_update(self):
        self.async_schedule_update_ha_state(True)

    # @property
    # def available(self) -> bool:
    #    """Return if entity is available."""
    #    if not (
    #        Profiles.SMART & self.central_system.get_supported_features(self.cpid)
    #    ):
    #        return False
    #    return self.central_system.get_available(self.cpid)  # type: ignore [no-any-return]

    async def async_set_native_value(self, value):
        """Set new value."""
        num_value = float(value)
        if self.central_system.get_available(
            self.cpid
        ) and Profiles.SMART & self.central_system.get_supported_features(self.cpid):
            resp = await self.central_system.set_max_charge_rate_amps(
                self.cpid, num_value
            )
            if resp is True:
                self._attr_native_value = num_value
                self.async_write_ha_state()
