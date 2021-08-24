"""Number platform for ocpp."""

from homeassistant.components.input_number import (
    CONF_INITIAL,
    CONF_MAX,
    CONF_MIN,
    CONF_STEP,
    MODE_BOX,
    MODE_SLIDER,
    InputNumber,
)
from homeassistant.const import (
    CONF_ICON,
    CONF_MODE,
    CONF_NAME,
    CONF_UNIT_OF_MEASUREMENT,
)
import homeassistant.helpers.config_validation as cv
import voluptuous as vol

from .api import CentralSystem
from .const import CONF_CPID, DEFAULT_CPID, DOMAIN, ICON


async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the slider platform."""
    central_system = hass.data[DOMAIN][entry.entry_id]
    cp_id = entry.data.get(CONF_CPID, DEFAULT_CPID)

    CREATE_FIELDS = {
        vol.Required(CONF_NAME, default=".".join([cp_id, "max_current"])): vol.All(
            str, vol.Length(min=1)
        ),
        vol.Required(CONF_MIN, default=0): vol.Coerce(float),
        vol.Required(CONF_MAX, default=32): vol.Coerce(float),
        vol.Optional(CONF_INITIAL, default=32): vol.Coerce(float),
        vol.Optional(CONF_STEP, default=1): vol.All(
            vol.Coerce(float), vol.Range(min=1e-3)
        ),
        vol.Optional(CONF_ICON, default=ICON): cv.icon,
        vol.Optional(CONF_UNIT_OF_MEASUREMENT, default="A"): cv.string,
        vol.Optional(CONF_MODE, default=MODE_SLIDER): vol.In([MODE_BOX, MODE_SLIDER]),
    }
    entities = []
    entities.append(
        ChargePointSlider(
            central_system,
            cp_id,
            CREATE_FIELDS,
        )
    )

    async_add_devices(entities, False)

    return True


class ChargePointSlider(InputNumber):
    """Individual slider for setting charge point maximum current."""

    def __init__(
        self,
        central_system: CentralSystem,
        cp_id: str,
        config: dict,
    ):
        """Initialize an input number."""
        self._config = config
        self.editable = True
        self._current_value = config.get(CONF_INITIAL)
        self.central_system = central_system
        self.cp_id = cp_id

    @property
    def unique_id(self):
        """Return the unique id of this slider."""
        return ".".join([DOMAIN, self.cp_id, "max_current", "slider"])

    @property
    def available(self) -> bool:
        """Return if slider is available."""
        return self.central_system.get_available(self.cp_id)

    @property
    def should_poll(self):
        """Return True if entity has to be polled for state.

        False if entity pushes its state to HA.
        """
        return True

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self.cp_id)},
            "via_device": (DOMAIN, self.central_system.id),
        }

    async def async_set_value(self, value):
        """Set new value."""
        num_value = float(value)

        if num_value < self._minimum or num_value > self._maximum:
            raise vol.Invalid(
                f"Invalid value for {self.entity_id}: {value} (range {self._minimum} - {self._maximum})"
            )
        if self.central_system.set_max_charge_rate_amps(self.cp_id, num_value):
            self._current_value = num_value
            self.async_write_ha_state()
