"""Switch platform for ocpp."""
from typing import Any

from homeassistant.components.switch import SwitchEntity

from .api import CentralSystem
from .const import CONF_CPID, DEFAULT_CPID, DOMAIN, ICON, SWITCHES


async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the sensor platform."""
    central_system = hass.data[DOMAIN][entry.entry_id]
    cp_id = entry.data.get(CONF_CPID, DEFAULT_CPID)

    entities = []

    for ent in SWITCHES:
        entities.append(ChargePointSwitch(central_system, cp_id, ent))

    async_add_devices(entities, False)


class ChargePointSwitch(SwitchEntity):
    """Individual switch for charge point."""

    def __init__(self, central_system: CentralSystem, cp_id: str, serv_desc):
        """Instantiate instance of a ChargePointSwitch."""
        self.cp_id = cp_id
        self._state = False
        self.central_system = central_system
        self._purpose = serv_desc
        if self._purpose.get("default") is not None:
            self._state = bool(self._purpose["default"])
        else:
            self._state = False
        self._id = ".".join(["switch", DOMAIN, self.cp_id, self._purpose["name"]])
        self._name = ".".join([self.cp_id, self._purpose["name"]])
        self.entity_id = "switch." + "_".join([self.cp_id, self._purpose["name"]])

    @property
    def unique_id(self):
        """Return the unique id of this entity."""
        return self._id

    @property
    def available(self) -> bool:
        """Return if switch is available."""
        return self.central_system.get_available(self.cp_id)  # type: ignore [no-any-return]

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on."""
        """Test metric state against condition if present"""
        if self._purpose.get("metric") is not None:
            resp = self.central_system.get_metric(self.cp_id, self._purpose["metric"])
            if resp == self._purpose["condition"]:
                self._state = True
            else:
                self._state = False
        return self._state  # type: ignore [no-any-return]

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        self._state = await self.central_system.set_charger_state(
            self.cp_id, self._purpose["on"]
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        """Response is True if successful but State is False"""
        if self._purpose.get("off") is None:
            resp = True
        elif self._purpose["off"] == self._purpose["on"]:
            resp = await self.central_system.set_charger_state(
                self.cp_id, self._purpose["off"], False
            )
        else:
            resp = await self.central_system.set_charger_state(
                self.cp_id, self._purpose["off"]
            )
        self._state = not resp

    @property
    def current_power_w(self) -> float:
        """Return the current power usage in W."""
        return self.central_system.get_metric(self.cp_id, "Power.Active.Import")

    @property
    def name(self):
        """Return the name of this entity."""
        return self._name

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

    async def async_update(self):
        """Get the latest data and update the states."""
        pass
