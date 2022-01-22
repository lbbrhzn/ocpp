"""Sensor platform for ocpp."""
from __future__ import annotations

from dataclasses import dataclass

import homeassistant
from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import CONF_MONITORED_VARIABLES
from homeassistant.helpers.entity import DeviceInfo, EntityCategory

from .api import CentralSystem
from .const import CONF_CPID, DEFAULT_CPID, DOMAIN, ICON, Measurand
from .enums import HAChargerDetails, HAChargerSession, HAChargerStatuses


@dataclass
class OcppSensorDescription(SensorEntityDescription):
    """Class to describe a Sensor entity."""

    scale: int = 1  # used for rounding metric


async def async_setup_entry(hass, entry, async_add_devices):
    """Configure the sensor platform."""
    central_system = hass.data[DOMAIN][entry.entry_id]
    cp_id = entry.data.get(CONF_CPID, DEFAULT_CPID)
    entities = []
    SENSORS = []
    for metric in list(
        set(entry.data[CONF_MONITORED_VARIABLES].split(",") + list(HAChargerSession))
    ):
        SENSORS.append(
            OcppSensorDescription(
                key=metric.lower(),
                name=metric,
            )
        )
    for metric in list(HAChargerStatuses) + list(HAChargerDetails):
        SENSORS.append(
            OcppSensorDescription(
                key=metric.lower(),
                name=metric,
                entity_category=EntityCategory.DIAGNOSTIC,
            )
        )

    for ent in SENSORS:
        entities.append(
            ChargePointMetric(
                central_system,
                cp_id,
                ent,
            )
        )

    async_add_devices(entities, False)


class ChargePointMetric(SensorEntity):
    """Individual sensor for charge point metrics."""

    entity_description: OcppSensorDescription

    def __init__(
        self,
        central_system: CentralSystem,
        cp_id: str,
        description: OcppSensorDescription,
    ):
        """Instantiate instance of a ChargePointMetrics."""
        self.central_system = central_system
        self.cp_id = cp_id
        self.entity_description = description
        self.metric = self.entity_description.name
        self._extra_attr = {}
        self._last_reset = homeassistant.util.dt.utc_from_timestamp(0)
        self._attr_unique_id = ".".join(
            [DOMAIN, self.cp_id, self.entity_description.key, SENSOR_DOMAIN]
        )
        self._attr_name = ".".join([self.cp_id, self.entity_description.name])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.cp_id)},
            via_device=(DOMAIN, self.central_system.id),
        )
        self.entity_id = (
            SENSOR_DOMAIN + "." + "_".join([self.cp_id, self.entity_description.key])
        )
        self._attr_icon = ICON

    @property
    def available(self) -> bool:
        """Return if sensor is available."""
        return self.central_system.get_available(self.cp_id)

    @property
    def should_poll(self):
        """Return True if entity has to be polled for state.

        False if entity pushes its state to HA.
        """
        return True

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return self.central_system.get_extra_attr(self.cp_id, self.metric)

    @property
    def state_class(self):
        """Return the state class of the sensor."""
        state_class = None
        if self.device_class is SensorDeviceClass.ENERGY:
            state_class = SensorStateClass.TOTAL_INCREASING
        elif (
            self.device_class
            in [
                SensorDeviceClass.CURRENT,
                SensorDeviceClass.VOLTAGE,
                SensorDeviceClass.POWER,
                SensorDeviceClass.TEMPERATURE,
                SensorDeviceClass.BATTERY,
                SensorDeviceClass.FREQUENCY,
            ]
            or self.metric == HAChargerStatuses.latency
        ):
            state_class = SensorStateClass.MEASUREMENT

        return state_class

    @property
    def device_class(self):
        """Return the device class of the sensor."""
        device_class = None
        if self.metric.lower().startswith("current."):
            device_class = SensorDeviceClass.CURRENT
        elif self.metric.lower().startswith("voltage"):
            device_class = SensorDeviceClass.VOLTAGE
        elif self.metric.lower().startswith("energy."):
            device_class = SensorDeviceClass.ENERGY
        elif (
            self.metric
            in [
                Measurand.frequency,
                Measurand.rpm,
            ]
            or self.metric.lower().startswith("frequency")
        ):
            device_class = SensorDeviceClass.FREQUENCY
        elif self.metric.lower().startswith("power."):
            device_class = SensorDeviceClass.POWER
        elif self.metric.lower().startswith("temperature."):
            device_class = SensorDeviceClass.TEMPERATURE
        elif self.metric.lower().startswith("timestamp.") or self.metric in [
            HAChargerDetails.config_response.value,
            HAChargerDetails.data_response.value,
            HAChargerStatuses.heartbeat.value,
        ]:
            device_class = SensorDeviceClass.TIMESTAMP
        elif self.metric.lower().startswith("soc"):
            device_class = SensorDeviceClass.BATTERY
        return device_class

    @property
    def native_value(self):
        """Return the state of the sensor, rounding if a number."""
        value = self.central_system.get_metric(self.cp_id, self.metric)
        if isinstance(value, float):
            value = round(value, self.entity_description.scale)
        return value

    @property
    def native_unit_of_measurement(self):
        """Return the native unit of measurement."""
        return self.central_system.get_ha_unit(self.cp_id, self.metric)
