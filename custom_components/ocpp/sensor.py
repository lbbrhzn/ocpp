"""Sensor platform for ocpp."""

import homeassistant
from homeassistant.components.sensor import (
    STATE_CLASS_MEASUREMENT,
    STATE_CLASS_TOTAL_INCREASING,
    SensorEntity,
)
from homeassistant.const import (
    CONF_MONITORED_VARIABLES,
    DEVICE_CLASS_CURRENT,
    DEVICE_CLASS_ENERGY,
    DEVICE_CLASS_POWER,
    DEVICE_CLASS_TEMPERATURE,
    DEVICE_CLASS_VOLTAGE,
)

from ocpp.v16.enums import UnitOfMeasure

from .api import CentralSystem
from .const import CONF_CPID, DEFAULT_CPID, DOMAIN, ICON
from .enums import HAChargerDetails, HAChargerSession, HAChargerStatuses


async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the sensor platform."""
    central_system = hass.data[DOMAIN][entry.entry_id]
    cp_id = entry.data.get(CONF_CPID, DEFAULT_CPID)

    entities = []

    for measurand in entry.data[CONF_MONITORED_VARIABLES].split(","):
        entities.append(
            ChargePointMetric(
                central_system,
                cp_id,
                measurand,
            )
        )
    for list in [HAChargerDetails, HAChargerSession, HAChargerStatuses]:
        for sensor in list:
            entities.append(
                ChargePointMetric(
                    central_system,
                    cp_id,
                    sensor.value,
                )
            )

    async_add_devices(entities, False)


class ChargePointMetric(SensorEntity):
    """Individual sensor for charge point metrics."""

    def __init__(
        self,
        central_system: CentralSystem,
        cp_id: str,
        metric: str,
    ):
        """Instantiate instance of a ChargePointMetrics."""
        self.central_system = central_system
        self.cp_id = cp_id
        self.metric = metric
        self._state = None
        self._extra_attr = {}
        self._last_reset = homeassistant.util.dt.utc_from_timestamp(0)

    @property
    def name(self):
        """Return the name of the sensor."""
        return ".".join([self.cp_id, self.metric])

    @property
    def unique_id(self):
        """Return the unique id of this sensor."""
        return ".".join([DOMAIN, self.cp_id, self.metric, "sensor"])

    @property
    def state(self):
        """Return the state of the sensor."""
        self._state = self.central_system.get_metric(self.cp_id, self.metric)
        return self._state

    @property
    def available(self) -> bool:
        """Return if sensor is available."""
        return self.central_system.get_available(self.cp_id)

    @property
    def unit_of_measurement(self):
        """Return the unit the value is expressed in."""
        return self.central_system.get_unit(self.cp_id, self.metric)

    @property
    def should_poll(self):
        """Return True if entity has to be polled for state.

        False if entity pushes its state to HA.
        """
        return True

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
        return self.central_system.get_extra_attr(self.cp_id, self.metric)

    @property
    def state_class(self):
        """Return the state class of the sensor."""
        if self.device_class is DEVICE_CLASS_ENERGY:
            state_class = STATE_CLASS_TOTAL_INCREASING
        else:
            state_class = STATE_CLASS_MEASUREMENT
        return state_class

    @property
    def device_class(self):
        """Return the device class of the sensor."""
        if self.unit_of_measurement in [
            UnitOfMeasure.wh.value,
            UnitOfMeasure.kwh.value,
        ]:
            return DEVICE_CLASS_ENERGY
        elif self.unit_of_measurement in [
            UnitOfMeasure.w.value,
            UnitOfMeasure.kw.value,
        ]:
            return DEVICE_CLASS_POWER
        elif self.unit_of_measurement in [
            UnitOfMeasure.celsius.value,
            UnitOfMeasure.fahrenheit.value,
        ]:
            return DEVICE_CLASS_TEMPERATURE
        elif self.unit_of_measurement in [UnitOfMeasure.a.value]:
            return DEVICE_CLASS_CURRENT
        elif self.unit_of_measurement in [UnitOfMeasure.v.value]:
            return DEVICE_CLASS_VOLTAGE
        else:
            return None

    async def async_update(self):
        """Get the latest data and update the states."""
        pass
