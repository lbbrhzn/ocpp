"""Number platform for ocpp."""
from homeassistant.components.input_number import InputNumber

from .api import CentralSystem
from .const import CONF_CPID, DEFAULT_CPID, DOMAIN, NUMBERS


async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the sensor platform."""
    central_system = hass.data[DOMAIN][entry.entry_id]
    cp_id = entry.data.get(CONF_CPID, DEFAULT_CPID)

    entities = []

    for cfg in NUMBERS:
        entities.append(Number(central_system, cp_id, cfg))

    async_add_devices(entities, False)


class Number(InputNumber):
    """Individual switch for charge point."""

    def __init__(self, central_system: CentralSystem, cp_id: str, config: dict):
        """Initialize a Number instance."""
        super().__init__(config)
        self.cp_id = cp_id
        self.central_system = central_system
        self.id = "number." + "_".join([self.cp_id, config["name"]])

    @property
    def unique_id(self):
        """Return the unique id of this entity."""
        return self.id

    @property
    def available(self) -> bool:
        """Return if switch is available."""
        return True  # self.central_system.get_available(self.cp_id)  # type: ignore [no-any-return]

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self.cp_id)},
            "via_device": (DOMAIN, self.central_system.id),
        }
