"""Switch platform for ocpp."""
from typing import Any

from homeassistant.components.switch import SwitchEntity

from .api import CentralSystem
from .const import CONF_CPID, DOMAIN, ICON


async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the sensor platform."""
    central_system = hass.data[DOMAIN][entry.entry_id]
    cp_id = entry.data[CONF_CPID]

    entities = []

    entities.append(
        ChargePointSwitch(
            central_system,
            cp_id,
        )
    )

    async_add_devices(entities, False)


class ChargePointSwitch(SwitchEntity):
    """Individual switch for charge point."""

    def __init__(self, central_system: CentralSystem, cp_id: str):
        """Instantiate instance of a ChargePointSwitch."""
        self.cp_id = cp_id
        self._state = False
        self.central_system = central_system

    @property
    def unique_id(self):
        """Return the unique id of this entity."""
        return ".".join([DOMAIN, self.cp_id, "switch"])

    @property
    def available(self) -> bool:
        """Return if switch is available."""
        return True  # type: ignore [no-any-return]

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on."""
        return self._state  # type: ignore [no-any-return]

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        self._state = True

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        self._state = False

    @property
    def current_power_w(self) -> float:
        """Return the current power usage in W."""
        return self.central_system.get_metric(self.cp_id, "Power.Active.Import")

    @property
    def name(self):
        """Return the name of this entity."""
        return "Switch"

    @property
    def icon(self):
        """Return the icon to use in the frontend, if any."""
        return ICON

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self.cp_id)},
            "via_device": (DOMAIN, self.central_system.id),
        }

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return {
            "unique_id": self.unique_id,
            "integration": DOMAIN,
        }

    def update(self):
        """Get the latest data and update the states."""
        pass
