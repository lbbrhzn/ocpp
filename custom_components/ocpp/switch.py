"""Switch platform for ocpp."""

from homeassistant.const import CONF_MONITORED_VARIABLES, CONF_NAME
from homeassistant.helpers.entity import Entity
from homeassistant.components.switch import SwitchEntity

from .const import CONDITIONS, DOMAIN, GENERAL, ICON, SERVICE_CHARGE_START, SERVICE_CHARGE_STOP, SERVICE_AVAILABILITY, SERVICE_RESET

# At a minimum define switch name and on service call, pulse used to call a service once such as reset
# metric and condition combination can be used to drive switch state, use default to set initial state to True 
SWITCH_CHARGE = {"name":"Charge_Control","on":SERVICE_CHARGE_START, "off":SERVICE_CHARGE_STOP, "metric": "Status", "condition":"Charging"}
SWITCH_AVAILABILITY = {"name":"Availability","on":SERVICE_AVAILABILITY, "off":SERVICE_AVAILABILITY, "default": True}
SWITCH_RESET = {"name":"Reset","on":SERVICE_RESET, "pulse": True}

async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the switch platform."""
    central_sys = hass.data[DOMAIN][entry.entry_id]

    entities = []
    entities.append(ChargePointSwitch(central_sys, entry.data[CONF_NAME], SWITCH_CHARGE))
    entities.append(ChargePointSwitch(central_sys, entry.data[CONF_NAME], SWITCH_AVAILABILITY))
    entities.append(ChargePointSwitch(central_sys, entry.data[CONF_NAME], SWITCH_RESET))
    async_add_devices(entities)


class ChargePointSwitch(SwitchEntity):
    """Individual switch for charge turning charge on/off."""

    def __init__(self, central_sys, prefix, serv_desc):
        """Instantiate instance of a ChargePointMetrics."""
        self.central_sys = central_sys
        self._prefix = prefix
        self._purpose = serv_desc
        if self._purpose.get("default") is not None:
            self._state = bool(self._purpose["default"])
        else:
            self._state = False
        self.type = "connected_chargers"
        self._id = ".".join(["switch", DOMAIN, self._prefix , self._purpose["name"]])
        self.entity_id = "switch." + "_".join([DOMAIN , self._prefix , self._purpose["name"]])

    @property
    def name(self):
        """Return the name of the switch."""
        return DOMAIN + "." + self._prefix + "." + self._purpose["name"]

    @property
    def unique_id(self):
        """Return the unique id of this switch."""
        return self._id

    @property
    def icon(self):
        """Return the icon to use in the frontend, if any."""
        return ICON

    @property
    def device_info(self):
        """Return device information."""
        return self.central_sys.device_info()

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return {
            "unique_id": self.unique_id,
            "integration": DOMAIN,
        }

    @property
    def is_on(self):
        """Return true if switch is on."""
        """Test metric state against condition if present"""
        if self._purpose.get("metric") is not None:
            if self.central_sys.get_metric(self._purpose["metric"]) == self.central_sys.get_metric(self._purpose["condition"]):
                self._state = True
            else:
                self._state = False
        return self._state

    async def async_turn_on(self, **kwargs):
        """Turn on device."""
        """For a pulse switch, reset to off afterwards"""
        if self._purpose.get("pulse",False) == True:
            resp = await self.central_sys.set_charger_state(self._purpose["on"])
            self._state = not resp
        else:
            self._state = await self.central_sys.set_charger_state(self._purpose["on"])

    async def async_turn_off(self, **kwargs):
        """Turn the device off."""
        """Response is True if successful but State is False"""
        if self._purpose.get("off") is None:
            resp = True
        elif self._purpose["off"] == self._purpose["on"]:
            resp = await self.central_sys.set_charger_state(self._purpose["off"], False)
        else:
            resp = await self.central_sys.set_charger_state(self._purpose["off"])
        self._state = not resp
