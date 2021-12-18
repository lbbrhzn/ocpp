"""Button platform for ocpp."""
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
from .const import CONF_CPID, DEFAULT_CPID, DOMAIN
from .enums import HAChargerServices


@dataclass
class OcppButtonDescription(ButtonEntityDescription):
    """Class to describe a Button entity."""

    press_action: str = ""


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
    cp_id = entry.data.get(CONF_CPID, DEFAULT_CPID)

    entities = []

    for ent in BUTTONS:
        entities.append(ChargePointButton(central_system, cp_id, ent))

    async_add_devices(entities, False)


class ChargePointButton(ButtonEntity):
    """Individual button for charge point."""

    entity_description: OcppButtonDescription

    def __init__(
        self,
        central_system: CentralSystem,
        cp_id: str,
        description: OcppButtonDescription,
    ):
        """Instantiate instance of a ChargePointButton."""
        self.cp_id = cp_id
        self.central_system = central_system
        self.entity_description = description
        self._attr_unique_id = ".".join(
            [BUTTON_DOMAIN, DOMAIN, self.cp_id, self.entity_description.key]
        )
        self._attr_name = ".".join([self.cp_id, self.entity_description.name])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.cp_id)},
            via_device=(DOMAIN, self.central_system.id),
        )
        self.entity_id = (
            BUTTON_DOMAIN + "." + "_".join([self.cp_id, self.entity_description.key])
        )

    @property
    def available(self) -> bool:
        """Return charger availability."""
        return self.central_system.get_available(self.cp_id)  # type: ignore [no-any-return]

    async def async_press(self) -> None:
        """Triggers the charger press action service."""
        await self.central_system.set_charger_state(
            self.cp_id, self.entity_description.press_action
        )
