"""Number platform for ocpp."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from homeassistant.components.number import (
    DOMAIN as NUMBER_DOMAIN,
    NumberEntity,
    NumberEntityDescription,
)
from homeassistant.helpers.entity import DeviceInfo
import voluptuous as vol

from .api import CentralSystem
from .const import CONF_CPID, DEFAULT_CPID, DOMAIN, ICON
from .enums import Profiles


@dataclass
class OcppNumberDescription(NumberEntityDescription):
    """Class to describe a Number entity."""

    initial_value: float | None = None
    # can be removed when dev branch released
    max_value: float | None = None
    min_value: float | None = None
    step: float | None = None


NUMBERS: Final = [
    NumberEntityDescription(
        key="maximum_current",
        name="Maximum_Current",
        icon=ICON,
        initial_value=32,
        min_value=0,
        max_value=32,
        step=1,
    ),
]


async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the number platform."""
    central_system = hass.data[DOMAIN][entry.entry_id]
    cp_id = entry.data.get(CONF_CPID, DEFAULT_CPID)

    entities = []

    for ent in NUMBERS:
        entities.append(OcppNumber(central_system, cp_id, ent))

    async_add_devices(entities, False)


class OcppNumber(NumberEntity):
    """Individual slider for setting charge rate."""

    entity_description: OcppNumberDescription

    def __init__(
        self,
        central_system: CentralSystem,
        cp_id: str,
        description: OcppNumberDescription,
    ):
        """Initialize a Number instance."""
        self.cp_id = cp_id
        self.central_system = central_system
        self.entity_description = description
        self._attr_unique_id = ".".join(
            [NUMBER_DOMAIN, self.cp_id, self.entity_description.key]
        )
        self._name = ".".join([self.cp_id, self.entity_description.name])
        self.entity_id = (
            NUMBER_DOMAIN + "." + "_".join([self.cp_id, self.entity_description.key])
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.cp_id)},
            via_device=(DOMAIN, self.central_system.id),
        )
        self._attr_value = self.entity_description.initial_value
        # can be removed when dev branch released
        self._attr_max_value = self.entity_description.max_value
        self._attr_min_value = self.entity_description.min_value
        self._attr_step = self.entity_description.step

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        if not (
            Profiles.SMART & self.central_system.get_supported_features(self.cp_id)
        ):
            return False
        return self.central_system.get_available(self.cp_id)  # type: ignore [no-any-return]

    async def async_set_value(self, value):
        """Set new value."""
        num_value = float(value)

        if num_value < self._attr_min_value or num_value > self._attr_max_value:
            raise vol.Invalid(
                f"Invalid value for {self.entity_id}: {value} (range {self._attr_min_value} - {self._attr_max_value})"
            )

        resp = await self.central_system.set_max_charge_rate_amps(self.cp_id, num_value)
        if resp is True:
            self._attr_value = num_value
            self.async_write_ha_state()
