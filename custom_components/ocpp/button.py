"""Button platform for ocpp."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from homeassistant.components.button import (
    DOMAIN as BUTTON_DOMAIN,
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.helpers.entity import DeviceInfo, EntityCategory

from .api import CentralSystem
from .const import CONF_CPID, CONF_CPIDS, DOMAIN
from .enums import HAChargerServices


@dataclass
class OcppButtonDescription(ButtonEntityDescription):
    """Class to describe a Button entity."""

    press_action: str | None = None


BUTTONS: Final = [
    OcppButtonDescription(
        key="reset",
        name="Reset",
        device_class=ButtonDeviceClass.RESTART,
        entity_category=EntityCategory.CONFIG,
        press_action=HAChargerServices.service_reset.name,
    ),
    OcppButtonDescription(
        key="unlock",
        name="Unlock",
        device_class=ButtonDeviceClass.UPDATE,
        entity_category=EntityCategory.CONFIG,
        press_action=HAChargerServices.service_unlock.name,
    ),
]


async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the Button platform."""

    central_system = hass.data[DOMAIN][entry.entry_id]
    for charger in entry.data[CONF_CPIDS]:
        cp_id_settings = list(charger.values())[0]
        cpid = cp_id_settings[CONF_CPID]

        entities = []

        for ent in BUTTONS:
            cpx = ChargePointButton(central_system, cpid, ent)
            # Only add if entity does not exist
            if hass.states.get(cpx._attr_unique_id) is None:
                entities.append(cpx)

    async_add_devices(entities, False)


class ChargePointButton(ButtonEntity):
    """Individual button for charge point."""

    _attr_has_entity_name = True
    entity_description: OcppButtonDescription

    def __init__(
        self,
        central_system: CentralSystem,
        cpid: str,
        description: OcppButtonDescription,
    ):
        """Instantiate instance of a ChargePointButton."""
        self.cpid = cpid
        self.central_system = central_system
        self.entity_description = description
        self._attr_unique_id = ".".join(
            [BUTTON_DOMAIN, DOMAIN, self.cpid, self.entity_description.key]
        )
        self._attr_name = self.entity_description.name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.cpid)},
        )

    @property
    def available(self) -> bool:
        """Return charger availability."""
        return self.central_system.get_available(self.cpid)  # type: ignore [no-any-return]

    async def async_press(self) -> None:
        """Triggers the charger press action service."""
        await self.central_system.set_charger_state(
            self.cpid, self.entity_description.press_action
        )
